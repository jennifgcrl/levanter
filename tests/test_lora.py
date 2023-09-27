import tempfile

import equinox as eqx
import jax
import numpy as np
from transformers import AutoModelForCausalLM

import haliax as hax
import haliax.nn as hnn

from levanter.compat.hf_checkpoints import HFCheckpointConverter
from levanter.lora import (
    LoraConfig,
    LoraLinear,
    lora_state_dict,
    loraize,
    merge_lora_modules,
    save_merged_hf_model,
    save_peft_pretrained,
)
from levanter.models.gpt2 import Gpt2Config, Gpt2LMHeadModel
from levanter.utils.tree_utils import inference_mode
from test_utils import skip_if_no_torch


In = hax.Axis("In", 10)
Mid = hax.Axis("Mid", 20)
Out = hax.Axis("Out", 5)


def test_loraize_simple():

    k0 = jax.random.PRNGKey(0)
    k1 = jax.random.PRNGKey(1)

    class Module(eqx.Module):
        first: hnn.Linear
        second: hnn.Linear

        def __call__(self, x):
            return self.second(self.first(x))

    module = Module(first=hnn.Linear.init(In, Mid, key=k0), second=hnn.Linear.init(Mid, Out, key=k1))

    loraized = loraize(module, LoraConfig(r=8, target_modules=["first"]), key=k0)
    assert isinstance(loraized.first, LoraLinear)
    assert isinstance(loraized.second, hnn.Linear)

    loraized = loraize(module, LoraConfig(r=8, target_modules=["second"]), key=k0)
    assert isinstance(loraized.first, hnn.Linear)
    assert isinstance(loraized.second, LoraLinear)

    input = hax.random.normal(k0, (In,))
    assert not hax.all(hax.isclose(module(input), loraized(input)))


def test_lora_scan_layers():
    class Module(eqx.Module):
        first: hnn.Linear
        second: hnn.Linear

        def __call__(self, x):
            return self.second(self.first(x))

        @staticmethod
        def init(*, key):
            k1, k2 = jax.random.split(key)
            first = hnn.Linear.init(In, Mid, key=k1)
            second = hnn.Linear.init(Mid, In, key=k2)
            return Module(first, second)

    Layers = hax.Axis("Layers", 3)

    k0 = jax.random.PRNGKey(0)
    module: hnn.Stacked[Module] = hnn.Stacked.init(Layers, Module)(key=jax.random.split(k0, 3))

    loraized = loraize(module, LoraConfig(r=8, target_modules=["first"]), key=k0)
    assert isinstance(loraized, hnn.Stacked)
    assert isinstance(loraized.stacked.first, LoraLinear)
    assert isinstance(loraized.stacked.second, hnn.Linear)

    assert loraized.stacked.first.lora.lora_A.weight.axes == (Layers, hax.Axis("LORA_R", 8), In)
    assert loraized.stacked.first.lora.lora_B.weight.axes == (Layers, Mid, hax.Axis("LORA_R", 8))

    assert loraized.stacked.second.weight.axes == (Layers, Mid, In)
    input = hax.random.normal(k0, (In,))
    assert not hax.all(hax.isclose(module.fold(input), loraized.fold(input)))


@skip_if_no_torch
def test_lora_peft_integration():
    import peft
    from transformers import AutoModelForCausalLM

    base_hf_model = AutoModelForCausalLM.from_pretrained("stanford-crfm/expanse-gpt2-small-x777")
    peft_config = peft.tuners.LoraConfig(
        base_model_name_or_path="stanford-crfm/expanse-gpt2-small-x777",
        peft_type="lora",
    )
    model = peft.get_peft_model(base_hf_model, peft_config)

    from peft.utils.save_and_load import get_peft_model_state_dict

    hf_dict = get_peft_model_state_dict(model)

    converter = Gpt2Config.default_hf_checkpoint_converter
    lev_model = converter.load_pretrained(Gpt2LMHeadModel, "stanford-crfm/expanse-gpt2-small-x777")

    lora_lev_model = loraize(lev_model, LoraConfig(r=8, target_modules=["c_attn"]), key=jax.random.PRNGKey(0))
    # for some dumb reason, the hf state dict starts with this prefix
    lev_dict = lora_state_dict(lora_lev_model)

    assert lev_dict.keys() == hf_dict.keys()

    for k, v in lev_dict.items():
        assert v.shape == hf_dict[k].shape


def test_merge_lora():
    class Module(eqx.Module):
        first: hnn.Linear
        second: hnn.Linear

        def __call__(self, x):
            return self.second(self.first(x))

        @staticmethod
        def init(*, key):
            k1, k2 = jax.random.split(key)
            first = hnn.Linear.init(In, Mid, key=k1)
            second = hnn.Linear.init(Mid, In, key=k2)
            return Module(first, second)

    Layers = hax.Axis("Layers", 3)

    k0 = jax.random.PRNGKey(0)
    module: hnn.Stacked[Module] = hnn.Stacked.init(Layers, Module)(key=jax.random.split(k0, 3))

    loraized = loraize(module, LoraConfig(r=8, target_modules=["first"]), key=k0)
    assert isinstance(loraized, hnn.Stacked)

    merged = merge_lora_modules(loraized)

    assert isinstance(merged, hnn.Stacked)

    input = hax.random.normal(k0, (In,))
    assert hax.all(hax.isclose(loraized.fold(input), merged.fold(input)))


@skip_if_no_torch
def test_lora_load_in_peft():
    import torch

    converter: HFCheckpointConverter = Gpt2Config.default_hf_checkpoint_converter
    config = Gpt2Config(seq_len=128, num_layers=2, num_heads=2)
    Vocab = converter.Vocab

    model = Gpt2LMHeadModel.init(Vocab, config=config, key=jax.random.PRNGKey(0))
    model = inference_mode(model, True)

    input = hax.random.randint(jax.random.PRNGKey(0), config.Pos, 0, Vocab.size)
    torch_input = torch.tensor(np.array(input.array), dtype=torch.long).reshape((1, -1))

    causal_mask = hax.nn.attention.causal_mask(model.Pos, config.KeyPos)

    with tempfile.TemporaryDirectory() as tmpdir:
        from peft import PeftConfig, PeftModel

        converter.save_pretrained(model, f"{tmpdir}/model")

        lora_config = LoraConfig(r=8, target_modules=["c_attn"])
        loraized = loraize(model, lora_config, key=jax.random.PRNGKey(0))
        save_peft_pretrained(loraized, lora_config, f"{tmpdir}/model", f"{tmpdir}/loraized")
        peft_config = PeftConfig.from_pretrained(f"{tmpdir}/loraized")

        hf_model = AutoModelForCausalLM.from_pretrained(peft_config.base_model_name_or_path).cpu()
        hf_model.eval()
        hf_out = hf_model(torch_input)
        hf_out = hf_out.logits.detach().numpy()

        lev_out = model(input, attn_mask=causal_mask)
        lev_out = np.array(lev_out.array)
        assert np.allclose(lev_out, hf_out, atol=1e-4)

        # load with peft
        hf_lora_model = PeftModel.from_pretrained(hf_model, f"{tmpdir}/loraized").cpu()

        lev_lora_out = loraized(input, attn_mask=causal_mask)
        lev_lora_out = np.array(lev_lora_out.array)

        hf_lora_model.eval()
        hf_lora_out = hf_lora_model(torch_input)
        hf_lora_out = hf_lora_out.logits.detach().numpy()

        assert np.allclose(lev_lora_out, hf_lora_out, atol=1e-4)
        assert not np.allclose(lev_lora_out, hf_out, atol=1e-4)


@skip_if_no_torch
def test_lora_merged_load_in_hf():
    import torch

    converter: HFCheckpointConverter = Gpt2Config.default_hf_checkpoint_converter
    config = Gpt2Config(seq_len=128, num_layers=2, num_heads=2)
    Vocab = converter.Vocab

    model = Gpt2LMHeadModel.init(Vocab, config=config, key=jax.random.PRNGKey(0))
    model = inference_mode(model, True)

    input = hax.random.randint(jax.random.PRNGKey(0), config.Pos, 0, Vocab.size)
    torch_input = torch.tensor(np.array(input.array), dtype=torch.long).reshape((1, -1))

    causal_mask = hax.nn.attention.causal_mask(model.Pos, config.KeyPos)

    with (tempfile.TemporaryDirectory() as tmpdir):
        converter.save_pretrained(model, f"{tmpdir}/model")

        lora_config = LoraConfig(r=8, target_modules=["c_attn"])
        loraized = loraize(model, lora_config, key=jax.random.PRNGKey(0))
        save_merged_hf_model(loraized, converter, f"{tmpdir}/loraized")

        hf_model = AutoModelForCausalLM.from_pretrained(f"{tmpdir}/model").cpu()
        hf_model.eval()
        hf_out = hf_model(torch_input)
        hf_out = hf_out.logits.detach().numpy()

        lev_out = model(input, attn_mask=causal_mask)
        lev_out = np.array(lev_out.array)
        assert np.allclose(lev_out, hf_out, atol=1e-4)

        # load merged model with hf
        hf_lora_model = AutoModelForCausalLM.from_pretrained(f"{tmpdir}/loraized").cpu()

        lev_lora_out = loraized(input, attn_mask=causal_mask)
        lev_lora_out = np.array(lev_lora_out.array)

        hf_lora_model.eval()
        hf_lora_out = hf_lora_model(torch_input)
        hf_lora_out = hf_lora_out.logits.detach().numpy()

        assert np.allclose(lev_lora_out, hf_lora_out, atol=1e-4)
        assert not np.allclose(lev_lora_out, hf_out, atol=1e-4)
