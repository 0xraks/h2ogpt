import functools
import inspect
import sys
import os
import traceback
import typing
from utils import set_seed, flatten_list, clear_torch_cache, system_info_print, zip_data, save_generate_output, s3up

SEED = 1236
set_seed(SEED)

os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
from typing import Union
import numpy as np
import pandas as pd

import fire
import torch
from peft import PeftModel
from transformers import GenerationConfig, StoppingCriteriaList, AutoModel
from accelerate import init_empty_weights, infer_auto_device_map

from prompter import Prompter

from finetune import get_loaders, example_data_points, generate_prompt, get_githash, prompt_types_strings, \
    human, bot, prompt_type_to_model_name, inv_prompt_type_to_model_lower
from stopping import CallbackToGenerator, Stream, StoppingCriteriaSub
from h2o_gradio_theme import h2o_theme

is_hf = bool(os.getenv("HUGGINGFACE_SPACES"))
is_gpth2oai = bool(os.getenv("GPT_H2O_AI"))
is_public = is_hf or is_gpth2oai  # multi-user case with fixed model and disclaimer
is_low_mem = is_hf  # assumes run on 24GB consumer GPU
admin_pass = os.getenv("ADMIN_PASS")
# will sometimes appear in UI or sometimes actual generation, but maybe better than empty result
raise_generate_gpu_exceptions = True

eval_extra_columns = ['prompt', 'response', 'score']

def main(
        load_8bit: bool = False,
        load_half: bool = True,
        infer_devices: bool = True,
        base_model: str = '',
        tokenizer_base_model: str = '',
        lora_weights: str = "",
        gpu_id: int = 0,  # if infer_devices = True and gpu_id != -1

        prompt_type: Union[int, str] = None,
        # input to generation
        temperature: float = None,
        top_p: float = None,
        top_k: int = None,
        num_beams: int = None,
        repetition_penalty: float = None,
        num_return_sequences: int = None,
        do_sample: bool = None,
        max_new_tokens: int = None,
        min_new_tokens: int = None,
        early_stopping: Union[bool, str] = None,
        max_time: float = None,

        llama_type: bool = None,
        debug: bool = False,
        save_dir: str = None,
        share: bool = True,
        local_files_only: bool = False,
        resume_download: bool = True,
        use_auth_token: Union[str, bool] = False,  # True requires CLI did huggingface-cli login before running

        src_lang: str = "English",
        tgt_lang: str = "Russian",

        gradio: bool = True,
        gradio_avoid_processing_markdown: bool = False,
        chat: bool = True,
        chat_history: int = 4096,  # character length of chat context/history
        stream_output: bool = True,
        show_examples: bool = None,
        verbose: bool = False,
        height: int = 400,
        show_lora: bool = True,
        # set to True to load --base_model after client logs in,
        # to be able to free GPU memory when model is swapped
        login_mode_if_model0: bool = False,
        block_gradio_exit: bool = True,
        concurrency_count: int = 1,
        api_open: bool = False,  # don't let API skip queue
        allow_api: bool = True,

        sanitize_user_prompt: bool = True,
        sanitize_bot_response: bool = True,

        extra_model_options: typing.List[str] = [],
        extra_lora_options: typing.List[str] = [],

        score_model: str = 'OpenAssistant/reward-model-deberta-v3-large-v2',
        auto_score: bool = True,

        eval_sharegpt_prompts_only: int = 0,
        eval_sharegpt_prompts_only_seed: int = 1234,
        eval_sharegpt_as_output: bool = False,
):
    # allow set token directly
    use_auth_token = os.environ.get("HUGGINGFACE_API_TOKEN", use_auth_token)

    if is_public:
        temperature = 0.4
        top_p = 0.85
        top_k = 70
        do_sample = True
        if is_low_mem:
            base_model = 'h2oai/h2ogpt-oasst1-512-12b'
            load_8bit = True
        else:
            base_model = 'h2oai/h2ogpt-oasst1-512-20b'
    if is_low_mem:
        load_8bit = True
    if is_hf:
        # must override share if in spaces
        share = False
    save_dir = os.getenv('SAVE_DIR', save_dir)
    score_model = os.getenv('SCORE_MODEL', score_model)
    if score_model == 'None':
        score_model = ''
    concurrency_count = int(os.getenv('CONCURRENCY_COUNT', concurrency_count))
    api_open = bool(int(os.getenv('API_OPEN', api_open)))
    allow_api = bool(int(os.getenv('ALLOW_API', allow_api)))

    # get defaults
    model_lower = base_model.lower()
    if not gradio:
        # force, else not single response like want to look at
        stream_output = False
        # else prompt removal can mess up output
        chat = False

    placeholder_instruction, placeholder_input, \
    stream_output, show_examples, \
    prompt_type, temperature, top_p, top_k, num_beams, \
    max_new_tokens, min_new_tokens, early_stopping, max_time, \
    repetition_penalty, num_return_sequences, \
    do_sample, \
    src_lang, tgt_lang, \
    examples, \
    task_info = \
        get_generate_params(model_lower, chat,
                            stream_output, show_examples,
                            prompt_type, temperature, top_p, top_k, num_beams,
                            max_new_tokens, min_new_tokens, early_stopping, max_time,
                            repetition_penalty, num_return_sequences,
                            do_sample,
                            )

    if not gradio:
        if eval_sharegpt_prompts_only > 0:
            # override default examples with shareGPT ones for human-level eval purposes only
            eval_filename = 'ShareGPT_V3_unfiltered_cleaned_split_no_imsorry.json'
            if not os.path.isfile(eval_filename):
                os.system(
                    'wget https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/%s' % eval_filename)
            import json
            data = json.load(open(eval_filename, 'rt'))
            # focus on data that starts with human, else likely chopped from other data
            turn_start = 0  # odd in general
            data = [x for x in data if len(x['conversations']) > turn_start + 1 and
                    x['conversations'][turn_start]['from'] == 'human' and
                    x['conversations'][turn_start + 1]['from'] == 'gpt']
            np.random.seed(eval_sharegpt_prompts_only_seed)
            example1 = examples[-1]  # pick reference example
            examples = []
            responses = []
            for i in list(np.random.randint(0, len(data), size=eval_sharegpt_prompts_only)):
                assert data[i]['conversations'][turn_start]['from'] == 'human'
                instruction = data[i]['conversations'][turn_start]['value']
                assert data[i]['conversations'][turn_start + 1]['from'] == 'gpt'
                output = data[i]['conversations'][turn_start + 1]['value']
                examplenew = example1.copy()
                assert not chat, "No gradio must use chat=False, uses nochat instruct"
                examplenew[eval_func_param_names.index('instruction_nochat')] = instruction
                examplenew[eval_func_param_names.index('iinput_nochat')] = ''  # no input
                examplenew[eval_func_param_names.index('context')] = ''  # no context
                examples.append(examplenew)
                responses.append(output)

        num_examples = len(examples)
        scoring_path = 'scoring'
        os.makedirs(scoring_path, exist_ok=True)
        if eval_sharegpt_as_output:
            used_base_model = 'gpt35'
            used_lora_weights = ''
        else:
            used_base_model = str(base_model.split('/')[-1])
            used_lora_weights = str(lora_weights.split('/')[-1])
        eval_filename = "df_scores_%s_%s_%s_%s_%s_%s.parquet" % (num_examples, eval_sharegpt_prompts_only,
                                                                 eval_sharegpt_prompts_only_seed,
                                                                 eval_sharegpt_as_output,
                                                                 used_base_model,
                                                                 used_lora_weights)
        eval_filename = os.path.join(scoring_path, eval_filename)

        with torch.device("cuda"):
            # ensure was set right above before examples generated
            assert not stream_output, "stream_output=True does not make sense with example loop"
            import time
            from functools import partial

            # get score model
            smodel, stokenizer, sdevice = get_score_model(**locals())

            if not eval_sharegpt_as_output:
                model, tokenizer, device = get_model(**locals())
                model_state = [model, tokenizer, device, base_model]
                fun = partial(evaluate, model_state, debug=debug, save_dir=save_dir)
            else:
                assert eval_sharegpt_prompts_only > 0

                def get_response(*args, exi=0):
                    # assumes same ordering of examples and responses
                    yield responses[exi]

                fun = get_response
            t0 = time.time()
            score_dump = []

            import matplotlib.pyplot as plt

            for exi, ex in enumerate(examples):
                instruction = ex[eval_func_param_names.index('instruction_nochat')]
                iinput = ex[eval_func_param_names.index('iinput_nochat')]
                context = ex[eval_func_param_names.index('context')]
                clear_torch_cache()
                print("")
                print("START" + "=" * 100)
                print("Question: %s %s" % (instruction, ('input=%s' % iinput if iinput else '')))
                print("-" * 105)
                # fun yields as generator, so have to iterate over it
                # Also means likely do NOT want --stream_output=True, else would show all generations
                for res in fun(*tuple(ex), exi=exi):
                    print(res)
                    if smodel:
                        score_with_prompt = False
                        if score_with_prompt:
                            data_point = dict(instruction=instruction, input=iinput, context=context)
                            prompter = Prompter(prompt_type, debug=debug, chat=chat, stream_output=stream_output)
                            prompt = prompter.generate_prompt(data_point)
                        else:
                            # just raw input and output
                            assert iinput in [None, '']  # should be no iinput
                            assert context in [None, '']  # should be no context
                            prompt = instruction
                        cutoff_len = 768 if is_low_mem else 2048
                        inputs = stokenizer(prompt, res,
                                            return_tensors="pt",
                                            truncation=True,
                                            max_length=cutoff_len)
                        try:
                            score = torch.sigmoid(smodel(**inputs).logits[0]).cpu().detach().numpy()[0]
                        except torch.cuda.OutOfMemoryError as e:
                            print("GPU OOM: question: %s answer: %s exception: %s" % (prompt, res, str(e)), flush=True)
                            traceback.print_exc()
                            score = 0.0
                            clear_torch_cache()
                        except (Exception, RuntimeError) as e:
                            if 'Expected all tensors to be on the same device' in str(e) or \
                                    'expected scalar type Half but found Float' in str(e) or \
                                    'probability tensor contains either' in str(e) or \
                                    'cublasLt ran into an error!' in str(e):
                                print("GPU error: question: %s answer: %s exception: %s" % (prompt, res, str(e)),
                                      flush=True)
                                traceback.print_exc()
                                score = 0.0
                                clear_torch_cache()
                            else:
                                raise
                        print("SCORE %s: %s" % (exi, score), flush=True)
                        score_dump.append(ex + [prompt, res, score])
                        # dump every score in case abort
                        df_scores = pd.DataFrame(score_dump,
                                                 columns=eval_func_param_names + eval_extra_columns)
                        df_scores.to_parquet(eval_filename, index=False)
                        # plot histogram so far
                        plt.figure(figsize=(10, 10))
                        plt.hist(df_scores['score'], bins=20)
                        score_avg = np.mean(df_scores['score'])
                        score_median = np.median(df_scores['score'])
                        plt.title("Score avg: %s median: %s" % (score_avg, score_median))
                        plt.savefig(eval_filename.replace('.parquet', '.png'))
                        plt.close()

                print("END" + "=" * 102)
                print("")
                t2 = time.time()
                print("Time taken so far: %.4f about %.4g per example" % (t2 - t0, (t2 - t0) / (1 + exi)))
            t1 = time.time()
            print("Total time taken: %.4f about %.4g per example" % (t1 - t0, (t1 - t0) / num_examples))
        return eval_filename

    if gradio:
        go_gradio(**locals())


def get_device():
    if torch.cuda.is_available():
        device = "cuda"
    else:
        raise RuntimeError("only cuda supported")

    return device


def get_non_lora_model(base_model, model_loader, load_half, model_kwargs, reward_type,
                       gpu_id=0,
                       use_auth_token=False):
    """
    Ensure model gets on correct device
    :param base_model:
    :param model_loader:
    :param load_half:
    :param model_kwargs:
    :param reward_type:
    :param gpu_id:
    :param use_auth_token:
    :return:
    """
    with init_empty_weights():
        from transformers import AutoConfig
        config = AutoConfig.from_pretrained(base_model, use_auth_token=use_auth_token)
        model = AutoModel.from_config(
            config,
        )

    # NOTE: Can specify max_memory={0: max_mem, 1: max_mem}, to shard model
    # NOTE: Some models require avoiding sharding some layers,
    # then would pass no_split_module_classes and give list of those layers.
    device_map = infer_auto_device_map(
        model,
        dtype=torch.float16 if load_half else torch.float32,
    )
    if hasattr(model, 'model'):
        device_map_model = infer_auto_device_map(
            model.model,
            dtype=torch.float16 if load_half else torch.float32,
        )
        device_map.update(device_map_model)
    print('device_map: %s' % device_map, flush=True)

    if gpu_id >= 0:
        # FIXME: If really distributes model, tend to get things like: ValueError: gpt_neox.embed_in.weight doesn't have any device set.
        # So avoid for now, just put on first GPU, unless score_model, put on last
        n_gpus = torch.cuda.device_count()
        if reward_type:
            device_map = {'': n_gpus - 1}
        else:
            device_map = {'': min(n_gpus - 1, gpu_id)}

    load_in_8bit = model_kwargs.get('load_in_8bit', False)
    model_kwargs['device_map'] = device_map

    if load_in_8bit or not load_half:
        model = model_loader.from_pretrained(
            base_model,
            **model_kwargs,
        )
    else:
        model = model_loader.from_pretrained(
            base_model,
            **model_kwargs,
        ).half()
    return model


def get_model(
        load_8bit: bool = False,
        load_half: bool = True,
        infer_devices: bool = True,
        base_model: str = '',
        tokenizer_base_model: str = '',
        lora_weights: str = "",
        gpu_id: int = 0,

        llama_type: bool = None,
        reward_type: bool = None,
        local_files_only: bool = False,
        resume_download: bool = True,
        use_auth_token: Union[str, bool] = False,
        compile: bool = True,
        **kwargs,
):
    """

    :param load_8bit: load model in 8-bit, not supported by all models
    :param load_half: load model in 16-bit
    :param infer_devices: Use torch infer of optimal placement of layers on devices (for non-lora case)
           For non-LORA case, False will spread shards across multiple GPUs, but this can lead to cuda:x cuda:y mismatches
           So it is not the default
    :param base_model: name/path of base model
    :param tokenizer_base_model: name/path of tokenizer
    :param lora_weights: name/path
    :param gpu_id: which GPU (0..n_gpus-1) or allow all GPUs if relevant (-1)
    :param llama_type: whether LLaMa type model
    :param reward_type: reward type model for sequence classification
    :param local_files_only: use local files instead of from HF
    :param resume_download: resume downloads from HF
    :param use_auth_token: assumes user did on CLI `huggingface-cli login` to access private repo
    :parm compile: whether to compile torch model
    :param kwargs:
    :return:
    """
    print("Get %s model" % base_model, flush=True)
    if lora_weights is not None and lora_weights.strip():
        print("Get %s lora weights" % lora_weights, flush=True)
    device = get_device()

    if 'gpt2' in base_model.lower():
        # RuntimeError: where expected condition to be a boolean tensor, but got a tensor with dtype Half
        load_8bit = False

    assert base_model.strip(), (
        "Please choose a base model with --base_model (CLI) or in Models Tab (gradio)"
    )
    llama_type = llama_type or "llama" in base_model
    model_loader, tokenizer_loader = get_loaders(llama_type=llama_type, model_name=base_model, reward_type=reward_type)
    if not tokenizer_base_model:
        tokenizer_base_model = base_model

    if tokenizer_loader is not None and not isinstance(tokenizer_loader, str):
        tokenizer = tokenizer_loader.from_pretrained(tokenizer_base_model,
                                                     local_files_only=local_files_only,
                                                     resume_download=resume_download,
                                                     use_auth_token=use_auth_token,
                                                     )
    else:
        tokenizer = tokenizer_loader

    if isinstance(tokenizer, str):
        # already a pipeline, tokenizer_loader is string for task
        model = model_loader(tokenizer,
                             model=base_model,
                             device=0 if device == "cuda" else -1,
                             torch_dtype=torch.float16)
    else:
        assert device == "cuda", "Unsupported device %s" % device
        model_kwargs = dict(local_files_only=local_files_only,
                            torch_dtype=torch.float16,
                            resume_download=resume_download,
                            use_auth_token=use_auth_token)
        if 'mbart-' not in base_model.lower():
            model_kwargs.update(dict(load_in_8bit=load_8bit,
                                     device_map={"": 0} if load_8bit else "auto",
                                     ))
        if 'OpenAssistant/reward-model'.lower() in base_model.lower():
            # could put on other GPUs
            model_kwargs['device_map'] = {"": 0}
            model_kwargs.pop('torch_dtype', None)

        if not lora_weights:
            with torch.device("cuda"):
                if infer_devices:
                    model = get_non_lora_model(base_model, model_loader, load_half, model_kwargs, reward_type,
                                               gpu_id=gpu_id, use_auth_token=use_auth_token)
                else:
                    if load_half and not load_8bit:
                        model = model_loader.from_pretrained(
                            base_model,
                            **model_kwargs).half()
                    else:
                        model = model_loader.from_pretrained(
                            base_model,
                            **model_kwargs)
        elif load_8bit:
            model = model_loader.from_pretrained(
                base_model,
                **model_kwargs
            )
            model = PeftModel.from_pretrained(
                model,
                lora_weights,
                torch_dtype=torch.float16,
                local_files_only=local_files_only,
                resume_download=resume_download,
                use_auth_token=use_auth_token,
                device_map={"": 0},  # seems to be required
            )
        else:
            with torch.device("cuda"):
                model = model_loader.from_pretrained(
                    base_model,
                    **model_kwargs
                )
                model = PeftModel.from_pretrained(
                    model,
                    lora_weights,
                    torch_dtype=torch.float16,
                    local_files_only=local_files_only,
                    resume_download=resume_download,
                    use_auth_token=use_auth_token,
                    device_map="auto",
                )
                if load_half:
                    model.half()

    # unwind broken decapoda-research config
    if llama_type:
        model.config.pad_token_id = tokenizer.pad_token_id = 0  # unk
        model.config.bos_token_id = 1
        model.config.eos_token_id = 2
    if 'gpt2' in base_model.lower():
        # add special tokens that otherwise all share the same id
        tokenizer.add_special_tokens({'bos_token': '<bos>',
                                      'eos_token': '<eos>',
                                      'pad_token': '<pad>'})

    if not isinstance(tokenizer, str):
        model.eval()
        if torch.__version__ >= "2" and sys.platform != "win32" and compile:
            model = torch.compile(model)

    return model, tokenizer, device


def get_score_model(**kwargs):
    # score model
    if kwargs.get('score_model') is not None and kwargs.get('score_model').strip():
        score_all_kwargs = kwargs.copy()
        score_all_kwargs['load_8bit'] = False
        score_all_kwargs['load_half'] = False
        score_all_kwargs['base_model'] = kwargs.get('score_model').strip()
        score_all_kwargs['tokenizer_base_model'] = ''
        score_all_kwargs['lora_weights'] = ''
        score_all_kwargs['llama_type'] = False
        score_all_kwargs['compile'] = False
        smodel, stokenizer, sdevice = get_model(**score_all_kwargs)
    else:
        smodel, stokenizer, sdevice = None, None, None
    return smodel, stokenizer, sdevice


def go_gradio(**kwargs):
    # get default model
    allow_api = kwargs['allow_api']
    all_kwargs = kwargs.copy()
    all_kwargs.update(locals())
    if kwargs.get('base_model') and not kwargs['login_mode_if_model0']:
        model0, tokenizer0, device = get_model(**all_kwargs)
    else:
        # if empty model, then don't load anything, just get gradio up
        model0, tokenizer0, device = None, None, None
    model_state0 = [model0, tokenizer0, device, kwargs['base_model']]

    # get score model
    smodel, stokenizer, sdevice = get_score_model(**all_kwargs)

    if 'mbart-' in kwargs['model_lower']:
        instruction_label_nochat = "Text to translate"
    else:
        instruction_label_nochat = "Instruction"
    instruction_label = "You (Shift-Enter or push Submit to send message)"

    title = 'h2oGPT'
    if kwargs['verbose']:
        description = f"""Model {kwargs['base_model']} Instruct dataset.
                      For more information, visit [the project's website](https://github.com/h2oai/h2ogpt).
                      Command: {str(' '.join(sys.argv))}
                      Hash: {get_githash()}
                      """
    else:
        description = "For more information, visit [the project's website](https://github.com/h2oai/h2ogpt).<br>"
    if is_public:
        description += "If this host is busy, try [gpt.h2o.ai 20B](https://gpt.h2o.ai) and [HF Spaces1 12B](https://huggingface.co/spaces/h2oai/h2ogpt-chatbot) and [HF Spaces2 12B](https://huggingface.co/spaces/h2oai/h2ogpt-chatbot2)<br>"
        description += """<p><b> DISCLAIMERS: </b><ul><i><li>The model was trained on The Pile and other data, which may contain objectionable content.  Use at own risk.</i></li>"""
        if kwargs['load_8bit']:
            description += """<i><li> Model is loaded in 8-bit and has other restrictions on this host. UX can be worse than non-hosted version.</i></li>"""
        description += """<i><li>Conversations may be used to improve h2oGPT.  Do not share sensitive information.</i></li>"""
        description += """<i><li>By using h2oGPT, you accept our [Terms of Service](https://github.com/h2oai/h2ogpt/blob/main/tos.md).</i></li></ul></p>"""

    if kwargs['verbose']:
        task_info_md = f"""
        ### Task: {kwargs['task_info']}"""
    else:
        task_info_md = ''

    css_code = """footer {visibility: hidden;}
body{background:linear-gradient(#f5f5f5,#e5e5e5);}
body.dark{background:linear-gradient(#000000,#0d0d0d);}
"""

    import gradio as gr

    if kwargs['gradio_avoid_processing_markdown']:
        from gradio_client import utils as client_utils
        from gradio.components import Chatbot

        # gradio has issue with taking too long to process input/output for markdown etc.
        # Avoid for now, allow raw html to render, good enough for chatbot.
        def _postprocess_chat_messages(self, chat_message: str):
            if chat_message is None:
                return None
            elif isinstance(chat_message, (tuple, list)):
                filepath = chat_message[0]
                mime_type = client_utils.get_mimetype(filepath)
                filepath = self.make_temp_copy_if_needed(filepath)
                return {
                    "name": filepath,
                    "mime_type": mime_type,
                    "alt_text": chat_message[1] if len(chat_message) > 1 else None,
                    "data": None,  # These last two fields are filled in by the frontend
                    "is_file": True,
                }
            elif isinstance(chat_message, str):
                return chat_message
            else:
                raise ValueError(f"Invalid message for Chatbot component: {chat_message}")

        Chatbot._postprocess_chat_messages = _postprocess_chat_messages

    dark_js = """() => {
        if (document.querySelectorAll('.dark').length) {
            document.querySelectorAll('.dark').forEach(el => el.classList.remove('dark'));
        } else {
            document.querySelector('body').classList.add('dark');
        }
    }"""
    demo = gr.Blocks(theme=h2o_theme, css=css_code, title="h2oGPT", analytics_enabled=False)
    callback = gr.CSVLogger()
    # css_code = 'body{background-image:url("https://h2o.ai/content/experience-fragments/h2o/us/en/site/header/master/_jcr_content/root/container/header_copy/logo.coreimg.svg/1678976605175/h2o-logo.svg");}'
    # demo = gr.Blocks(theme='gstaff/xkcd', css=css_code)

    model_options = flatten_list(list(prompt_type_to_model_name.values())) + kwargs['extra_model_options']
    if kwargs['base_model'].strip() not in model_options:
        lora_options = [kwargs['base_model'].strip()] + model_options
    lora_options = kwargs['extra_lora_options']
    if kwargs['lora_weights'].strip() not in lora_options:
        lora_options = [kwargs['lora_weights'].strip()] + lora_options
    # always add in no lora case
    # add fake space so doesn't go away in gradio dropdown
    no_lora_str = no_model_str = '[None/Remove]'
    lora_options = [no_lora_str] + kwargs['extra_lora_options']  # FIXME: why double?
    # always add in no model case so can free memory
    # add fake space so doesn't go away in gradio dropdown
    model_options = [no_model_str] + model_options

    # transcribe, will be detranscribed before use by evaluate()
    if not kwargs['lora_weights'].strip():
        kwargs['lora_weights'] = no_lora_str

    if not kwargs['base_model'].strip():
        kwargs['base_model'] = no_model_str

    # transcribe for gradio
    kwargs['gpu_id'] = str(kwargs['gpu_id'])

    no_model_msg = 'h2oGPT [   !!! Please Load Model in Models Tab !!!   ]'
    output_label0 = f'h2oGPT [Model: {kwargs.get("base_model")}]' if kwargs.get(
        'base_model') else no_model_msg
    output_label0_model2 = no_model_msg

    h2o_logo = '<svg id="Layer_1" data-name="Layer 1" xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"' \
               ' viewBox="0 0 600.28 600.28"><defs><style>.cls-1{fill:#fec925;}.cls-2{fill:#161616;}.cls-3{fill:' \
               '#54585a;}</style></defs><g id="Fill-1"><rect class="cls-1" width="600.28" height="600.28" ' \
               'rx="23.24"/></g><path class="cls-2" d="M174.33,246.06v92.78H152.86v-38H110.71v38H89.24V246.06h21.' \
               '47v36.58h42.15V246.06Z"/><path class="cls-2" d="M259.81,321.34v17.5H189.7V324.92l35.78-33.8c8.22-7.' \
               '82,9.68-12.59,9.68-17.09,0-7.29-5-11.53-14.85-11.53-7.95,0-14.71,3-19.21,9.27L185.46,261.7c7.15-10' \
               '.47,20.14-17.23,36.84-17.23,20.68,0,34.46,10.6,34.46,27.44,0,9-2.52,17.22-15.51,29.29l-21.33,20.14Z"' \
               '/><path class="cls-2" d="M268.69,292.45c0-27.57,21.47-48,50.76-48s50.76,20.28,50.76,48-21.6,48-50.' \
               '76,48S268.69,320,268.69,292.45Zm79.78,0c0-17.63-12.46-29.69-29-29.69s-29,12.06-29,29.69,12.46,29.69' \
               ',29,29.69S348.47,310.08,348.47,292.45Z"/><path class="cls-3" d="M377.23,326.91c0-7.69,5.7-12.73,12.' \
               '85-12.73s12.86,5,12.86,12.73a12.86,12.86,0,1,1-25.71,0Z"/><path class="cls-3" d="M481.4,298.15v40.' \
               '69H462.05V330c-3.84,6.49-11.27,9.94-21.74,9.94-16.7,0-26.64-9.28-26.64-21.61,0-12.59,8.88-21.34,30.' \
               '62-21.34h16.43c0-8.87-5.3-14-16.43-14-7.55,0-15.37,2.51-20.54,6.62l-7.43-14.44c7.82-5.57,19.35-8.' \
               '62,30.75-8.62C468.81,266.47,481.4,276.54,481.4,298.15Zm-20.68,18.16V309H446.54c-9.67,0-12.72,3.57-' \
               '12.72,8.35,0,5.16,4.37,8.61,11.66,8.61C452.37,326,458.34,322.8,460.72,316.31Z"/><path class="cls-3"' \
               ' d="M497.56,246.06c0-6.49,5.17-11.53,12.86-11.53s12.86,4.77,12.86,11.13c0,6.89-5.17,11.93-12.86,' \
               '11.93S497.56,252.55,497.56,246.06Zm2.52,21.47h20.68v71.31H500.08Z"/></svg>'

    with demo:
        # avoid actual model/tokenizer here or anything that would be bad to deepcopy
        # https://github.com/gradio-app/gradio/issues/3558
        model_state = gr.State(['model', 'tokenizer', device, kwargs['base_model']])
        model_state2 = gr.State([None, None, None, None])
        model_options_state = gr.State([model_options])
        lora_options_state = gr.State([lora_options])
        gr.Markdown(
            f"""
            <div style="display:flex; justify-content:center; margin-bottom:30px;">
                <div style="height: 60px; width: 60px; margin-right:20px;">{h2o_logo}</div>
                <h1 style="line-height:60px">{title}</h1>
            </div>
            
            {description}
            {task_info_md}
            """)
        if is_hf:
            gr.HTML(
                '''<center><a href="https://huggingface.co/spaces/h2oai/h2ogpt-chatbot?duplicate=true"><img src="https://bit.ly/3gLdBN6" alt="Duplicate Space"></a>Duplicate this Space to skip the queue and run in a private space</center>''')

        # go button visible if
        base_wanted = kwargs['base_model'] != no_model_str and kwargs['login_mode_if_model0']
        go_btn = gr.Button(value="ENTER", visible=base_wanted, variant="primary")
        normal_block = gr.Row(visible=not base_wanted)
        with normal_block:
            with gr.Tabs():
                with gr.Row():
                    col_nochat = gr.Column(visible=not kwargs['chat'])
                    with col_nochat:  # FIXME: for model comparison, and check rest
                        text_output_nochat = gr.Textbox(lines=5, label=output_label0)
                        instruction_nochat = gr.Textbox(
                            lines=4, label=instruction_label_nochat,
                            placeholder=kwargs['placeholder_instruction'],
                        )
                        iinput_nochat = gr.Textbox(lines=4, label="Input context for Instruction",
                                                   placeholder=kwargs['placeholder_input'])
                        submit_nochat = gr.Button("Submit")
                        flag_btn_nochat = gr.Button("Flag")
                        if not kwargs['auto_score']:
                            with gr.Column(visible=kwargs['score_model']):
                                score_btn_nochat = gr.Button("Score last prompt & response")
                                score_text_nochat = gr.Textbox("Response Score: NA", show_label=False)
                        else:
                            with gr.Column(visible=kwargs['score_model']):
                                score_text_nochat = gr.Textbox("Response Score: NA", show_label=False)
                    col_chat = gr.Column(visible=kwargs['chat'])
                    with col_chat:
                        with gr.Row():
                            text_output = gr.Chatbot(label=output_label0).style(height=kwargs['height'] or 400)
                            text_output2 = gr.Chatbot(label=output_label0_model2, visible=False).style(
                                height=kwargs['height'] or 400)
                        with gr.Row():
                            with gr.Column(scale=50):
                                instruction = gr.Textbox(
                                    lines=4, label=instruction_label,
                                    placeholder=kwargs['placeholder_instruction'],
                                )
                            with gr.Row():
                                submit = gr.Button(value='Submit').style(full_width=False, size='sm')
                                stop_btn = gr.Button(value="Stop").style(full_width=False, size='sm')
                        with gr.Row():
                            clear = gr.Button("New Conversation")
                            flag_btn = gr.Button("Flag")
                            if not kwargs['auto_score']:  # FIXME: For checkbox model2
                                with gr.Column(visible=kwargs['score_model']):
                                    with gr.Row():
                                        score_btn = gr.Button("Score last prompt & response").style(
                                            full_width=False, size='sm')
                                        score_text = gr.Textbox("Response Score: NA", show_label=False)
                                    score_res2 = gr.Row(visible=False)
                                    with score_res2:
                                        score_btn2 = gr.Button("Score last prompt & response 2").style(
                                            full_width=False, size='sm')
                                        score_text2 = gr.Textbox("Response Score2: NA", show_label=False)
                            else:
                                with gr.Column(visible=kwargs['score_model']):
                                    score_text = gr.Textbox("Response Score: NA", show_label=False)
                                    score_text2 = gr.Textbox("Response Score2: NA", show_label=False, visible=False)
                            retry = gr.Button("Regenerate")
                            undo = gr.Button("Undo")
                with gr.TabItem("Input/Output"):
                    with gr.Row():
                        if 'mbart-' in kwargs['model_lower']:
                            src_lang = gr.Dropdown(list(languages_covered().keys()),
                                                   value=kwargs['src_lang'],
                                                   label="Input Language")
                            tgt_lang = gr.Dropdown(list(languages_covered().keys()),
                                                   value=kwargs['tgt_lang'],
                                                   label="Output Language")
                with gr.TabItem("Expert"):
                    with gr.Row():
                        with gr.Column():
                            stream_output = gr.components.Checkbox(label="Stream output",
                                                                   value=kwargs['stream_output'])
                            prompt_type = gr.Dropdown(prompt_types_strings,
                                                      value=kwargs['prompt_type'], label="Prompt Type",
                                                      visible=not is_public)
                            prompt_type2 = gr.Dropdown(prompt_types_strings,
                                                       value=kwargs['prompt_type'], label="Prompt Type Model 2",
                                                       visible=not is_public and False)
                            do_sample = gr.Checkbox(label="Sample", info="Enable sampler, required for use of temperature, top_p, top_k",
                                                    value=kwargs['do_sample'])
                            temperature = gr.Slider(minimum=0.01, maximum=3,
                                                    value=kwargs['temperature'],
                                                    label="Temperature",
                                                    info="Lower is deterministic (but may lead to repeats), Higher more creative (but may lead to hallucinations)")
                            top_p = gr.Slider(minimum=0, maximum=1,
                                              value=kwargs['top_p'], label="Top p",
                                              info="Cumulative probability of tokens to sample from")
                            top_k = gr.Slider(
                                minimum=0, maximum=100, step=1,
                                value=kwargs['top_k'], label="Top k",
                                info='Num. tokens to sample from'
                            )
                            max_beams = 8 if not is_low_mem else 2
                            num_beams = gr.Slider(minimum=1, maximum=max_beams, step=1,
                                                  value=min(max_beams, kwargs['num_beams']), label="Beams",
                                                  info="Number of searches for optimal overall probability.  "
                                                       "Uses more GPU memory/compute")
                            max_max_new_tokens = 2048 if not is_low_mem else kwargs['max_new_tokens']
                            max_new_tokens = gr.Slider(
                                minimum=1, maximum=max_max_new_tokens, step=1,
                                value=min(max_max_new_tokens, kwargs['max_new_tokens']), label="Max output length",
                            )
                            min_new_tokens = gr.Slider(
                                minimum=0, maximum=max_max_new_tokens, step=1,
                                value=min(max_max_new_tokens, kwargs['min_new_tokens']), label="Min output length",
                            )
                            early_stopping = gr.Checkbox(label="EarlyStopping", info="Stop early in beam search",
                                                         value=kwargs['early_stopping'])
                            max_max_time = 60 * 5 if not is_low_mem else 60
                            max_time = gr.Slider(minimum=0, maximum=max_max_time, step=1,
                                                 value=min(max_max_time, kwargs['max_time']), label="Max. time",
                                                 info="Max. time to search optimal output.")
                            repetition_penalty = gr.Slider(minimum=0.01, maximum=3.0,
                                                           value=kwargs['repetition_penalty'],
                                                           label="Repetition Penalty")
                            num_return_sequences = gr.Slider(minimum=1, maximum=10, step=1,
                                                             value=kwargs['num_return_sequences'],
                                                             label="Number Returns", info="Must be <= num_beams",
                                                             visible=not is_public)
                            iinput = gr.Textbox(lines=4, label="Input",
                                                placeholder=kwargs['placeholder_input'],
                                                visible=not is_public)
                            context = gr.Textbox(lines=3, label="System Pre-Context",
                                                 info="Directly pre-appended without prompt processing",
                                                 visible=not is_public and not kwargs['chat'])
                            chat = gr.components.Checkbox(label="Chat mode", value=kwargs['chat'],
                                                          visible=not is_public)

                with gr.TabItem("Models"):
                    load_msg = "Load-Unload Model/LORA" if not is_public \
                        else "LOAD-UNLOAD DISABLED FOR HOSTED DEMO"
                    load_msg2 = "Load-Unload Model/LORA 2" if not is_public \
                        else "LOAD-UNLOAD DISABLED FOR HOSTED DEMO 2"
                    compare_checkbox = gr.components.Checkbox(label="Compare Mode",
                                                              value=False, visible=not is_public)
                    with gr.Row():
                        n_gpus = torch.cuda.device_count()
                        n_gpus_list = [str(x) for x in list(range(-1, n_gpus))]
                        with gr.Column():
                            with gr.Row():
                                with gr.Column(scale=50):
                                    model_choice = gr.Dropdown(model_options_state.value[0], label="Choose Model",
                                                               value=kwargs['base_model'])
                                    lora_choice = gr.Dropdown(lora_options_state.value[0], label="Choose LORA",
                                                              value=kwargs['lora_weights'], visible=kwargs['show_lora'])
                                with gr.Column(scale=1):
                                    load_model_button = gr.Button(load_msg)
                                    model_load8bit_checkbox = gr.components.Checkbox(
                                        label="Load 8-bit [Not all models support]",
                                        value=kwargs['load_8bit'])
                                    model_infer_devices_checkbox = gr.components.Checkbox(
                                        label="Infer Devices [If GPU ID=-1 or not Checked, then will spread model over GPUs]",
                                        value=kwargs['infer_devices'])
                                    model_gpu = gr.Dropdown(n_gpus_list, label="GPU ID [-1 = all GPUs]",
                                                            value=kwargs['gpu_id'])
                                    model_used = gr.Textbox(label="Current Model", value=kwargs['base_model'])
                                    lora_used = gr.Textbox(label="Current LORA", value=kwargs['lora_weights'],
                                                           visible=kwargs['show_lora'])
                            with gr.Row():
                                with gr.Column(scale=50):
                                    new_model = gr.Textbox(label="New Model HF name/path")
                                    new_lora = gr.Textbox(label="New LORA HF name/path", visible=kwargs['show_lora'])
                                with gr.Column(scale=1):
                                    add_model_button = gr.Button("Add new model name")
                                    add_lora_button = gr.Button("Add new LORA name", visible=kwargs['show_lora'])
                        col_model2 = gr.Column(visible=False)
                        with col_model2:
                            with gr.Row():
                                with gr.Column(scale=50):
                                    model_choice2 = gr.Dropdown(model_options_state.value[0], label="Choose Model 2",
                                                                value=no_model_str)
                                    lora_choice2 = gr.Dropdown(lora_options_state.value[0], label="Choose LORA 2",
                                                               value=no_lora_str,
                                                               visible=kwargs['show_lora'])
                                with gr.Column(scale=1):
                                    load_model_button2 = gr.Button(load_msg2)
                                    model_load8bit_checkbox2 = gr.components.Checkbox(
                                        label="Load 8-bit 2 [Not all models support]",
                                        value=kwargs['load_8bit'])
                                    model_infer_devices_checkbox2 = gr.components.Checkbox(
                                        label="Infer Devices 2 [If GPU ID=-1 or not Checked, then will spread model over GPUs]",
                                        value=kwargs[
                                            'infer_devices'])
                                    model_gpu2 = gr.Dropdown(n_gpus_list, label="GPU ID [-1 = all GPUs]",
                                                             value=kwargs['gpu_id'])
                                    # no model/lora loaded ever in model2 by default
                                    model_used2 = gr.Textbox(label="Current Model 2", value=no_model_str)
                                    lora_used2 = gr.Textbox(label="Current LORA 2", value=no_lora_str,
                                                            visible=kwargs['show_lora'])
                with gr.TabItem("System"):
                    admin_row = gr.Row()
                    with admin_row:
                        admin_pass_textbox = gr.Textbox(label="Admin Password", type='password', visible=is_public)
                        admin_btn = gr.Button(value="Admin Access", visible=is_public)
                    system_row = gr.Row(visible=not is_public)
                    with system_row:
                        with gr.Column():
                            with gr.Row():
                                system_btn = gr.Button(value='Get System Info')
                                system_text = gr.Textbox(label='System Info')

                            with gr.Row():
                                zip_btn = gr.Button("Zip")
                                zip_text = gr.Textbox(label="Zip file name")
                                file_output = gr.File()
                            with gr.Row():
                                s3up_btn = gr.Button("S3UP")
                                s3up_text = gr.Textbox(label='S3UP result')

        # Get flagged data
        zip_data1 = functools.partial(zip_data, root_dirs=['flagged_data_points', kwargs['save_dir']])
        zip_btn.click(zip_data1, inputs=None, outputs=[file_output, zip_text])
        s3up_btn.click(s3up, inputs=zip_text, outputs=s3up_text)

        def check_admin_pass(x):
            return gr.update(visible=x == admin_pass)

        def close_admin(x):
            return gr.update(visible=not (x == admin_pass))

        admin_btn.click(check_admin_pass, inputs=admin_pass_textbox, outputs=system_row) \
                 .then(close_admin, inputs=admin_pass_textbox, outputs=admin_row)

        # Get inputs to evaluate()
        inputs_list = get_inputs_list(locals(), kwargs['model_lower'])
        from functools import partial
        all_kwargs = kwargs.copy()
        all_kwargs.update(locals())
        kwargs_evaluate = {k: v for k, v in all_kwargs.items() if k in inputs_kwargs_list}
        fun = partial(evaluate,
                      **kwargs_evaluate)
        fun2 = partial(evaluate,
                       **kwargs_evaluate)

        dark_mode_btn = gr.Button("Dark Mode", variant="primary").style(
            size="sm",
        )
        dark_mode_btn.click(
            None,
            None,
            None,
            _js=dark_js,
            api_name="dark" if allow_api else None,
        )

        # Control chat and non-chat blocks, which can be independently used by chat checkbox swap
        def col_nochat_fun(x):
            return gr.Column.update(visible=not x)

        def col_chat_fun(x):
            return gr.Column.update(visible=x)

        def context_fun(x):
            return gr.Textbox.update(visible=not x)

        chat.select(col_nochat_fun, chat, col_nochat, api_name="chat_checkbox" if allow_api else None) \
            .then(col_chat_fun, chat, col_chat) \
            .then(context_fun, chat, context)

        # examples after submit or any other buttons for chat or no chat
        if kwargs['examples'] is not None and kwargs['show_examples']:
            gr.Examples(examples=kwargs['examples'], inputs=inputs_list)

        # Score
        def score_last_response(*args, nochat=False, model2=False):
            """ Similar to user() """
            args_list = list(args)

            max_length_tokenize = 512 if is_low_mem else 2048
            cutoff_len = max_length_tokenize * 4  # restrict deberta related to max for LLM

            if not nochat:
                history = args_list[-1]
                if history is None:
                    if not model2:
                        # maybe only doing first model, no need to complain
                        print("Bad history in scoring last response, fix for now", flush=True)
                    history = []
                if smodel is not None and \
                        stokenizer is not None and \
                        sdevice is not None and \
                        history is not None and len(history) > 0 and \
                        history[-1] is not None and \
                        len(history[-1]) >= 2:
                    os.environ['TOKENIZERS_PARALLELISM'] = 'false'

                    question = history[-1][0]

                    answer = history[-1][1]
                else:
                    return 'Response Score: NA'
            else:
                answer = args_list[-1]
                instruction_nochat_arg_id = eval_func_param_names.index('instruction_nochat')
                question = args_list[instruction_nochat_arg_id]

            if question is None:
                return 'Response Score: Bad Question'
            if answer is None:
                return 'Response Score: Bad Answer'

            question = question[-cutoff_len:]
            answer = answer[-cutoff_len:]

            inputs = stokenizer(question, answer,
                                return_tensors="pt",
                                truncation=True,
                                max_length=max_length_tokenize).to(smodel.device)
            try:
                score = torch.sigmoid(smodel(**inputs).logits[0]).cpu().detach().numpy()[0]
            except torch.cuda.OutOfMemoryError as e:
                print("GPU OOM: question: %s answer: %s exception: %s" % (question, answer, str(e)), flush=True)
                del inputs
                traceback.print_exc()
                clear_torch_cache()
                return 'Response Score: GPU OOM'
            except (Exception, RuntimeError) as e:
                if 'Expected all tensors to be on the same device' in str(e) or \
                        'expected scalar type Half but found Float' in str(e) or \
                        'probability tensor contains either' in str(e) or \
                        'cublasLt ran into an error!' in str(e):
                    print("GPU Error: question: %s answer: %s exception: %s" % (question, answer, str(e)),
                          flush=True)
                    traceback.print_exc()
                    clear_torch_cache()
                    return 'Response Score: GPU Error'
                else:
                    raise
            os.environ['TOKENIZERS_PARALLELISM'] = 'true'
            return 'Response Score: {:.1%}'.format(score)

        def noop_score_last_response(*args, **kwargs):
            return "Response Score: Disabled"
        if kwargs['score_model']:
            score_fun = score_last_response
        else:
            score_fun = noop_score_last_response

        score_args = dict(fn=score_fun,
                          inputs=inputs_list + [text_output],
                          outputs=[score_text],
                          )
        score_args2 = dict(fn=partial(score_fun, model2=True),
                           inputs=inputs_list + [text_output2],
                           outputs=[score_text2],
                           )

        score_args_nochat = dict(fn=partial(score_fun, nochat=True),
                                 inputs=inputs_list + [text_output_nochat],
                                 outputs=[score_text_nochat],
                                 )
        if not kwargs['auto_score']:
            score_event = score_btn.click(**score_args, queue=stream_output, api_name='score' if allow_api else None) \
                .then(**score_args2, queue=stream_output, api_name='score2' if allow_api else None)
            score_event_nochat = score_btn_nochat.click(**score_args_nochat, queue=stream_output,
                                                        api_name='score_nochat' if allow_api else None)

        def user(*args, undo=False, sanitize_user_prompt=True, model2=False):
            """
            User that fills history for bot
            :param args:
            :param undo:
            :param sanitize_user_prompt:
            :param model2:
            :return:
            """
            args_list = list(args)
            user_message = args_list[0]
            input1 = args_list[1]
            context1 = args_list[2]
            if input1 and not user_message.endswith(':'):
                user_message1 = user_message + ":" + input1
            elif input1:
                user_message1 = user_message + input1
            else:
                user_message1 = user_message
            if sanitize_user_prompt:
                from better_profanity import profanity
                user_message1 = profanity.censor(user_message1)

            history = args_list[-1]
            if undo and history:
                history.pop()
            args_list = args_list[:-1]  # FYI, even if unused currently
            if history is None:
                if not model2:
                    # no need to complain so often unless model1
                    print("Bad history, fix for now", flush=True)
                history = []
            # ensure elements not mixed across models as output,
            # even if input is currently same source
            history = history.copy()
            if undo:
                return history
            else:
                # FIXME: compare, same history for now
                return history + [[user_message1, None]]

        def bot(*args, retry=False):
            """
            bot that consumes history for user input
            instruction (from input_list) itself is not consumed by bot
            :param args:
            :param retry:
            :return:
            """
            args_list = list(args).copy()
            history = args_list[-1]  # model_state is -2
            if retry and history:
                history.pop()
            if not history:
                print("No history", flush=True)
                return
            # ensure output will be unique to models
            history = history.copy()
            instruction1 = history[-1][0]
            context1 = ''
            if kwargs['chat_history'] > 0:
                prompt_type_arg_id = eval_func_param_names.index('prompt_type')
                prompt_type1 = args_list[prompt_type_arg_id]
                chat_arg_id = eval_func_param_names.index('chat')
                chat1 = args_list[chat_arg_id]
                context1 = ''
                for histi in range(len(history) - 1):
                    data_point = dict(instruction=history[histi][0], input='', output=history[histi][1])
                    context1 += generate_prompt(data_point, prompt_type1, chat1, reduced=True)[0].replace(
                        '<br>', '\n')
                    if not context1.endswith('\n'):
                        context1 += '\n'
                if context1 and not context1.endswith('\n'):
                    context1 += '\n'  # ensure if terminates abruptly, then human continues on next line
            args_list[0] = instruction1  # override original instruction with history from user
            # only include desired chat history
            args_list[2] = context1[-kwargs['chat_history']:]
            model_state1 = args_list[-2]
            if model_state1[0] is None or model_state1[0] == no_model_str:
                return
            args_list = args_list[:-2]
            fun1 = partial(evaluate,
                           model_state1,
                           **kwargs_evaluate)
            try:
                for output in fun1(*tuple(args_list)):
                    bot_message = output
                    history[-1][1] = bot_message
                    yield history
            except StopIteration:
                yield history
            except RuntimeError as e:
                if "generator raised StopIteration" in str(e):
                    # assume last entry was bad, undo
                    history.pop()
                    yield history
                raise
            except Exception as e:
                # put error into user input
                history[-1][0] = "Exception: %s" % str(e)
                yield history
                raise
            return

        # NORMAL MODEL
        user_args = dict(fn=functools.partial(user, sanitize_user_prompt=kwargs['sanitize_user_prompt']),
                         inputs=inputs_list + [text_output],
                         outputs=text_output,
                         )
        bot_args = dict(fn=bot,
                        inputs=inputs_list + [model_state] + [text_output],
                        outputs=text_output,
                        )
        retry_bot_args = dict(fn=functools.partial(bot, retry=True),
                              inputs=inputs_list + [model_state] + [text_output],
                              outputs=text_output,
                              )
        undo_user_args = dict(fn=functools.partial(user, undo=True),
                              inputs=inputs_list + [text_output],
                              outputs=text_output,
                              )

        # MODEL2
        user_args2 = dict(fn=functools.partial(user, sanitize_user_prompt=kwargs['sanitize_user_prompt'], model2=True),
                          inputs=inputs_list + [text_output2],
                          outputs=text_output2,
                          )
        bot_args2 = dict(fn=bot,
                         inputs=inputs_list + [model_state2] + [text_output2],
                         outputs=text_output2,
                         )
        retry_bot_args2 = dict(fn=functools.partial(bot, retry=True),
                               inputs=inputs_list + [model_state2] + [text_output2],
                               outputs=text_output2,
                               )
        undo_user_args2 = dict(fn=functools.partial(user, undo=True),
                               inputs=inputs_list + [text_output2],
                               outputs=text_output2,
                               )

        def clear_instruct():
            return gr.Textbox.update(value='')

        if kwargs['auto_score']:
            # in case 2nd model, consume instruction first, so can clear quickly
            # bot doesn't consume instruction itself, just history from user, so why works
            submit_event = instruction.submit(**user_args, queue=stream_output, api_name='instruction' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='instruction2' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**bot_args, api_name='instruction_bot' if allow_api else None) \
                .then(**score_args, api_name='instruction_bot_score' if allow_api else None) \
                .then(**bot_args2, api_name='instruction_bot2' if allow_api else None) \
                .then(**score_args2, api_name='instruction_bot_score2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event2 = submit.click(**user_args, queue=stream_output, api_name='submit' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='submit2' if allow_api else None) \
                .then(**bot_args, api_name='submit_bot' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**score_args, api_name='submit_bot_score' if allow_api else None) \
                .then(**bot_args2, api_name='submit_bot2' if allow_api else None) \
                .then(**score_args2, api_name='submit_bot_score2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event3 = retry.click(**user_args, queue=stream_output, api_name='retry' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='retry2' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**retry_bot_args, api_name='retry_bot' if allow_api else None) \
                .then(**score_args, api_name='retry_bot_score' if allow_api else None) \
                .then(**retry_bot_args2, api_name='retry_bot2' if allow_api else None) \
                .then(**score_args2, api_name='retry_bot_score2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event4 = undo.click(**undo_user_args, queue=stream_output, api_name='undo' if allow_api else None) \
                .then(**score_args, api_name='undo_score' if allow_api else None) \
                .then(**undo_user_args2, queue=stream_output, api_name='undo2' if allow_api else None) \
                .then(**score_args2, api_name='undo_score2' if allow_api else None) \
                .then(clear_instruct, None, instruction)
        else:
            submit_event = instruction.submit(**user_args, queue=stream_output, api_name='instruction' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='instruction2' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**bot_args, api_name='instruction_bot' if allow_api else None) \
                .then(**bot_args2, api_name='instruction_bot2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event2 = submit.click(**user_args, queue=stream_output, api_name='submit' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='submit2' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**bot_args, api_name='submit_bot' if allow_api else None) \
                .then(**bot_args2, api_name='submit_bot2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event3 = retry.click(**user_args, queue=stream_output, api_name='retry' if allow_api else None) \
                .then(**user_args2, queue=stream_output, api_name='retry2' if allow_api else None) \
                .then(clear_instruct, None, instruction) \
                .then(**retry_bot_args, api_name='retry_bot' if allow_api else None) \
                .then(**retry_bot_args2, api_name='retry_bot2' if allow_api else None) \
                .then(clear_torch_cache)
            submit_event4 = undo.click(**undo_user_args, queue=stream_output, api_name='undo' if allow_api else None) \
                .then(**undo_user_args2, queue=stream_output, api_name='undo2' if allow_api else None)

        # does both models
        clear.click(lambda: None, None, text_output, queue=False, api_name='clear' if allow_api else None) \
            .then(lambda: None, None, text_output2, queue=False, api_name='clear2' if allow_api else None)
        # FIXME: compare
        submit_event_nochat = submit_nochat.click(fun, inputs=[model_state] + inputs_list,
                                                  outputs=text_output_nochat, api_name='submit_nochat' if allow_api else None) \
            .then(**score_args_nochat, api_name='instruction_bot_score_nochat' if allow_api else None) \
            .then(clear_torch_cache)

        def load_model(model_name, lora_weights, model_state_old, prompt_type_old, load_8bit, infer_devices, gpu_id):
            # ensure old model removed from GPU memory
            if kwargs['debug']:
                print("Pre-switch pre-del GPU memory: %s" % torch.cuda.memory_allocated(), flush=True)

            if isinstance(model_state_old[0], str) and model0 is not None:
                # best can do, move model loaded at first to CPU
                model0.cpu()

            if model_state_old[0] is not None and not isinstance(model_state_old[0], str):
                try:
                    model_state_old[0].cpu()
                except Exception as e:
                    # sometimes hit NotImplementedError: Cannot copy out of meta tensor; no data!
                    print("Unable to put model on CPU: %s" % str(e), flush=True)
                del model_state_old[0]
                model_state_old[0] = None

            if model_state_old[1] is not None and not isinstance(model_state_old[1], str):
                del model_state_old[1]
                model_state_old[1] = None

            clear_torch_cache()
            if kwargs['debug']:
                print("Pre-switch post-del GPU memory: %s" % torch.cuda.memory_allocated(), flush=True)

            if model_name is None or model_name == no_model_str:
                # no-op if no model, just free memory
                # no detranscribe needed for model, never go into evaluate
                lora_weights = no_lora_str
                return [None, None, None, model_name], model_name, lora_weights, prompt_type_old

            all_kwargs1 = all_kwargs.copy()
            all_kwargs1['base_model'] = model_name.strip()
            all_kwargs1['load_8bit'] = load_8bit
            all_kwargs1['infer_devices'] = infer_devices
            all_kwargs1['gpu_id'] = int(gpu_id)  # detranscribe
            model_lower = model_name.strip().lower()
            if model_lower in inv_prompt_type_to_model_lower:
                prompt_type1 = inv_prompt_type_to_model_lower[model_lower]
            else:
                prompt_type1 = prompt_type_old

            # detranscribe
            if lora_weights == no_lora_str:
                lora_weights = ''

            all_kwargs1['lora_weights'] = lora_weights.strip()
            model1, tokenizer1, device1 = get_model(**all_kwargs1)
            clear_torch_cache()

            if kwargs['debug']:
                print("Post-switch GPU memory: %s" % torch.cuda.memory_allocated(), flush=True)
            return [model1, tokenizer1, device1, model_name], model_name, lora_weights, prompt_type1

        def dropdown_prompt_type_list(x):
            return gr.Dropdown.update(value=x)

        def chatbot_list(x, model_used_in):
            return gr.Textbox.update(label=f'h2oGPT [Model: {model_used_in}]')

        load_model_args = dict(fn=load_model,
                               inputs=[model_choice, lora_choice, model_state, prompt_type,
                                       model_load8bit_checkbox, model_infer_devices_checkbox, model_gpu],
                               outputs=[model_state, model_used, lora_used, prompt_type])
        prompt_update_args = dict(fn=dropdown_prompt_type_list, inputs=prompt_type, outputs=prompt_type)
        chatbot_update_args = dict(fn=chatbot_list, inputs=[text_output, model_used], outputs=text_output)
        nochat_update_args = dict(fn=chatbot_list, inputs=[text_output, model_used], outputs=text_output_nochat)
        if not is_public:
            load_model_event = load_model_button.click(**load_model_args) \
                .then(**prompt_update_args) \
                .then(**chatbot_update_args) \
                .then(**nochat_update_args) \
                .then(clear_torch_cache)

        load_model_args2 = dict(fn=load_model,
                                inputs=[model_choice2, lora_choice2, model_state2, prompt_type2,
                                        model_load8bit_checkbox2, model_infer_devices_checkbox2, model_gpu2],
                                outputs=[model_state2, model_used2, lora_used2, prompt_type2])
        prompt_update_args2 = dict(fn=dropdown_prompt_type_list, inputs=prompt_type2, outputs=prompt_type2)
        chatbot_update_args2 = dict(fn=chatbot_list, inputs=[text_output2, model_used2], outputs=text_output2)
        if not is_public:
            load_model_event2 = load_model_button2.click(**load_model_args2) \
                .then(**prompt_update_args2) \
                .then(**chatbot_update_args2) \
                .then(clear_torch_cache)

        def dropdown_model_list(list0, x):
            new_state = [list0[0] + [x]]
            new_options = [*new_state[0]]
            return gr.Dropdown.update(value=x, choices=new_options), \
                   gr.Dropdown.update(value=x, choices=new_options), \
                   '', new_state

        add_model_event = add_model_button.click(fn=dropdown_model_list,
                                                 inputs=[model_options_state, new_model],
                                                 outputs=[model_choice, model_choice2, new_model, model_options_state])

        def dropdown_lora_list(list0, x, model_used1, lora_used1, model_used2, lora_used2):
            new_state = [list0[0] + [x]]
            new_options = [*new_state[0]]
            # don't switch drop-down to added lora if already have model loaded
            x1 = x if model_used1 == no_model_str else lora_used1
            x2 = x if model_used2 == no_model_str else lora_used2
            return gr.Dropdown.update(value=x1, choices=new_options), \
                   gr.Dropdown.update(value=x2, choices=new_options), \
                   '', new_state

        add_lora_event = add_lora_button.click(fn=dropdown_lora_list,
                                               inputs=[lora_options_state, new_lora, model_used, lora_used, model_used2, lora_used2],
                                               outputs=[lora_choice, lora_choice2, new_lora, lora_options_state])

        go_btn.click(lambda: gr.update(visible=False), None, go_btn, api_name="go" if allow_api else None) \
            .then(lambda: gr.update(visible=True), None, normal_block) \
            .then(**load_model_args).then(**prompt_update_args)

        def compare_textbox_fun(x):
            return gr.Textbox.update(visible=x)

        def compare_column_fun(x):
            return gr.Column.update(visible=x)

        def compare_prompt_fun(x):
            return gr.Dropdown.update(visible=x)

        compare_checkbox.select(compare_textbox_fun, compare_checkbox, text_output2,
                                api_name="compare_checkbox" if allow_api else None) \
            .then(compare_column_fun, compare_checkbox, col_model2) \
            .then(compare_prompt_fun, compare_checkbox, prompt_type2) \
            .then(compare_textbox_fun, compare_checkbox, score_text2)
        # FIXME: add score_res2 in condition, but do better

        # callback for logging flagged input/output
        callback.setup(inputs_list + [text_output], "flagged_data_points")
        flag_btn.click(lambda *args: callback.flag(args), inputs_list + [text_output], None, preprocess=False,
                       api_name='flag' if allow_api else None)
        flag_btn_nochat.click(lambda *args: callback.flag(args), inputs_list + [text_output], None, preprocess=False,
                              api_name='flag_nochat' if allow_api else None)

        def get_system_info():
            return gr.Textbox.update(value=system_info_print())

        system_event = system_btn.click(get_system_info, outputs=system_text, api_name='system_info' if allow_api else None)

        # don't pass text_output, don't want to clear output, just stop it
        # FIXME: have to click once to stop output and second time to stop GPUs going
        stop_btn.click(lambda: None, None, None,
                       cancels=[submit_event_nochat, submit_event, submit_event2, submit_event3],
                       queue=False, api_name='stop' if allow_api else None).then(clear_torch_cache)
        demo.load(None, None, None, _js=dark_js)

    demo.queue(concurrency_count=kwargs['concurrency_count'], api_open=kwargs['api_open'])
    favicon_path = "h2o-logo.svg"
    demo.launch(share=kwargs['share'], server_name="0.0.0.0", show_error=True,
                favicon_path=favicon_path, prevent_thread_lock=True)  # , enable_queue=True)
    print("Started GUI", flush=True)
    if kwargs['block_gradio_exit']:
        demo.block_thread()


input_args_list = ['model_state']
inputs_kwargs_list = ['debug', 'save_dir', 'hard_stop_list', 'sanitize_bot_response', 'model_state0']


def get_inputs_list(inputs_dict, model_lower):
    """
    map gradio objects in locals() to inputs for evaluate().
    :param inputs_dict:
    :param model_lower:
    :return:
    """
    inputs_list_names = list(inspect.signature(evaluate).parameters)
    inputs_list = []
    for k in inputs_list_names:
        if k == 'kwargs':
            continue
        if k in input_args_list + inputs_kwargs_list:
            # these are added via partial, not taken as input
            continue
        if 'mbart-' not in model_lower and k in ['src_lang', 'tgt_lang']:
            continue
        inputs_list.append(inputs_dict[k])
    return inputs_list


eval_func_param_names = ['instruction',
                         'iinput',
                         'context',
                         'stream_output',
                         'prompt_type',
                         'temperature',
                         'top_p',
                         'top_k',
                         'num_beams',
                         'max_new_tokens',
                         'min_new_tokens',
                         'early_stopping',
                         'max_time',
                         'repetition_penalty',
                         'num_return_sequences',
                         'do_sample',
                         'chat',
                         'instruction_nochat',
                         'iinput_nochat',
                         ]


def evaluate(
        model_state,
        # START NOTE: Examples must have same order of parameters
        instruction,
        iinput,
        context,
        stream_output,
        prompt_type,
        temperature,
        top_p,
        top_k,
        num_beams,
        max_new_tokens,
        min_new_tokens,
        early_stopping,
        max_time,
        repetition_penalty,
        num_return_sequences,
        do_sample,
        chat,
        instruction_nochat,
        iinput_nochat,
        # END NOTE: Examples must have same order of parameters
        src_lang=None,
        tgt_lang=None,
        debug=False,
        save_dir=None,
        hard_stop_list=None,
        sanitize_bot_response=True,
        model_state0=None,
        **kwargs,
):
    if debug:
        locals_dict = locals().copy()
        locals_dict.pop('model_state', None)
        locals_dict.pop('model_state0', None)
        print(locals_dict)

    no_model_msg = "Please choose a base model with --base_model (CLI) or in Models Tab (gradio).\nThen start New Conversation"

    if model_state0 is None:
        # e.g. for no gradio case, set dummy value, else should be set
        model_state0 = [None, None, None, None]

    if model_state is not None and len(model_state) == 4 and not isinstance(model_state[0], str):
        # try to free-up original model (i.e. list was passed as reference)
        if model_state0 is not None and model_state0[0] is not None:
            model_state0[0].cpu()
            model_state0[0] = None
        # try to free-up original tokenizer (i.e. list was passed as reference)
        if model_state0 is not None and model_state0[1] is not None:
            model_state0[1] = None
        clear_torch_cache()
        model, tokenizer, device, base_model = model_state
    elif model_state0 is not None and len(model_state0) == 4 and model_state0[0] is not None:
        assert isinstance(model_state[0], str)
        model, tokenizer, device, base_model = model_state0
    else:
        raise AssertionError(no_model_msg)

    if base_model is None:
        raise AssertionError(no_model_msg)

    assert base_model.strip(), no_model_msg
    assert model, "Model is missing"
    assert tokenizer, "Tokenizer is missing"

    # choose chat or non-chat mode
    if not chat:
        instruction = instruction_nochat
        iinput = iinput_nochat

    data_point = dict(context=context, instruction=instruction, input=iinput)
    prompter = Prompter(prompt_type, debug=debug, chat=chat, stream_output=stream_output)
    prompt = prompter.generate_prompt(data_point)

    if hard_stop_list is None:
        # acts like undo on user entry and bot response
        hard_stop_list = []

    if isinstance(tokenizer, str):
        # pipeline
        if tokenizer == "summarization":
            key = 'summary_text'
        else:
            raise RuntimeError("No such task type %s" % tokenizer)
        # NOTE: uses max_length only
        yield model(prompt, max_length=max_new_tokens)[0][key]

    if 'mbart-' in base_model.lower():
        assert src_lang is not None
        tokenizer.src_lang = languages_covered()[src_lang]

    if chat:
        # override, ignore user change
        num_return_sequences = 1
    if prompt_type in ['human_bot', 'instruct_vicuna', 'instruct_with_end']:
        if prompt_type == 'human_bot':
            # encounters = [prompt.count(human) + 1, prompt.count(bot) + 1]
            # stopping only starts once output is beyond prompt
            # 1 human is enough to trigger, but need 2 bots, because very first view back will be bot we added
            stop_words = [human, bot, '\n' + human, '\n' + bot]
            encounters = [1, 2]
        elif prompt_type == 'instruct_vicuna':
            # even below is not enough, generic strings and many ways to encode
            stop_words = [
                '### Human:',
                """
### Human:""",
                """
### Human:
""",
                '### Assistant:',
                """
### Assistant:""",
                """
### Assistant:
""",
            ]
            encounters = [1, 2]
        else:
            # some instruct prompts have this as end, doesn't hurt to stop on it since not common otherwise
            stop_words = ['### End']
            encounters = [1]
        stop_words_ids = [
            tokenizer(stop_word, return_tensors='pt')['input_ids'].squeeze() for stop_word in stop_words]
        # handle single token case
        stop_words_ids = [x if len(x.shape) > 0 else torch.tensor([x]) for x in stop_words_ids]
        stop_words_ids = [x for x in stop_words_ids if x.shape[0] > 0]
        # avoid padding in front of tokens
        if tokenizer.pad_token:
            stop_words_ids = [x[1:] if x[0] == tokenizer.pad_token_id and len(x) > 1 else x for x in stop_words_ids]
        # handle fake \n added
        stop_words_ids = [x[1:] if y[0] == '\n' else x for x, y in zip(stop_words_ids, stop_words)]
        # build stopper
        stopping_criteria = StoppingCriteriaList([StoppingCriteriaSub(stops=stop_words_ids, encounters=encounters)])
    else:
        stopping_criteria = StoppingCriteriaList()

    # help to avoid errors like:
    # RuntimeError: The size of tensor a (2048) must match the size of tensor b (2049) at non-singleton dimension 3
    # RuntimeError: expected scalar type Half but found Float
    # with - 256
    max_length_tokenize = 768 - 256 if is_low_mem else 2048 - 256
    cutoff_len = max_length_tokenize * 4  # if reaches limit, then can't generate new tokens
    output_smallest = 30 * 4
    prompt = prompt[-cutoff_len - output_smallest:]
    inputs = tokenizer(prompt,
                       return_tensors="pt",
                       truncation=True,
                       max_length=max_length_tokenize)
    if debug and len(inputs["input_ids"]) > 0:
        print('input_ids length', len(inputs["input_ids"][0]), flush=True)
    input_ids = inputs["input_ids"].to(device)
    generation_config = GenerationConfig(
        temperature=float(temperature),
        top_p=float(top_p),
        top_k=top_k,
        num_beams=num_beams,
        do_sample=do_sample,
        repetition_penalty=float(repetition_penalty),
        num_return_sequences=num_return_sequences,
        renormalize_logits=True,
        remove_invalid_values=True,
        **kwargs,
    )

    gen_kwargs = dict(input_ids=input_ids,
                      generation_config=generation_config,
                      return_dict_in_generate=True,
                      output_scores=True,
                      max_new_tokens=max_new_tokens,  # prompt + new
                      min_new_tokens=min_new_tokens,  # prompt + new
                      early_stopping=early_stopping,  # False, True, "never"
                      max_time=max_time,
                      stopping_criteria=stopping_criteria,
                      )
    if 'gpt2' in base_model.lower():
        gen_kwargs.update(dict(bos_token_id=tokenizer.bos_token_id, pad_token_id=tokenizer.eos_token_id))
    elif 'mbart-' in base_model.lower():
        assert tgt_lang is not None
        tgt_lang = languages_covered()[tgt_lang]
        gen_kwargs.update(dict(forced_bos_token_id=tokenizer.lang_code_to_id[tgt_lang]))
    else:
        gen_kwargs.update(dict(pad_token_id=tokenizer.eos_token_id))

    decoder = functools.partial(tokenizer.decode,
                                skip_special_tokens=True,
                                clean_up_tokenization_spaces=True,
                                )
    decoder_raw = functools.partial(tokenizer.decode,
                                    skip_special_tokens=False,
                                    clean_up_tokenization_spaces=True,
                                    )

    with torch.no_grad():
        # decoded tokenized prompt can deviate from prompt due to special characters
        inputs_decoded = decoder(input_ids[0])
        inputs_decoded_raw = decoder_raw(input_ids[0])
        if inputs_decoded == prompt:
            # normal
            pass
        elif inputs_decoded.lstrip() == prompt.lstrip():
            # sometimes extra space in front, make prompt same for prompt removal
            prompt = inputs_decoded
        elif inputs_decoded_raw == prompt:
            # some models specify special tokens that are part of normal prompt, so can't skip them
            inputs_decoded_raw = inputs_decoded
            decoder = decoder_raw
        else:
            print("WARNING: Special characters in prompt", flush=True)
        if stream_output:
            def generate(callback=None, **kwargs):
                # re-order stopping so Stream first and get out all chunks before stop for other reasons
                stopping_criteria0 = kwargs.get('stopping_criteria', StoppingCriteriaList()).copy()
                kwargs['stopping_criteria'] = StoppingCriteriaList()
                kwargs['stopping_criteria'].append(Stream(func=callback))
                for stopping_criteria1 in stopping_criteria0:
                    kwargs['stopping_criteria'].append(stopping_criteria1)

                try:
                    model.generate(**kwargs)
                except torch.cuda.OutOfMemoryError as e:
                    print("GPU OOM: prompt: %s inputs_decoded: %s exception: %s" % (prompt, inputs_decoded, str(e)),
                          flush=True)
                    if kwargs['input_ids'] is not None:
                        kwargs['input_ids'].cpu()
                    kwargs['input_ids'] = None
                    traceback.print_exc()
                    clear_torch_cache()
                    return
                except (Exception, RuntimeError) as e:
                    if 'Expected all tensors to be on the same device' in str(e) or \
                            'expected scalar type Half but found Float' in str(e) or \
                            'probability tensor contains either' in str(e) or \
                            'cublasLt ran into an error!' in str(e):
                        print(
                            "GPU Error: prompt: %s inputs_decoded: %s exception: %s" % (prompt, inputs_decoded, str(e)),
                            flush=True)
                        traceback.print_exc()
                        clear_torch_cache()
                        if raise_generate_gpu_exceptions:
                            raise
                        return
                    else:
                        raise

            decoded_output = None
            for output in CallbackToGenerator(generate, callback=None, **gen_kwargs):
                decoded_output = decoder(output)
                if output[-1] in [tokenizer.eos_token_id]:
                    if debug:
                        print("HIT EOS", flush=True)
                    break
                if any(ele in decoded_output for ele in hard_stop_list):
                    raise StopIteration
                yield prompter.get_response(decoded_output, prompt=inputs_decoded,
                                            sanitize_bot_response=sanitize_bot_response)
            if save_dir and decoded_output:
                save_generate_output(output=decoded_output, base_model=base_model, save_dir=save_dir)
        else:
            outputs = model.generate(**gen_kwargs)
            outputs = [decoder(s) for s in outputs.sequences]
            yield prompter.get_response(outputs, prompt=inputs_decoded,
                                        sanitize_bot_response=sanitize_bot_response)
            if save_dir and outputs and len(outputs) >= 1:
                decoded_output = prompt + outputs[0]
                save_generate_output(output=decoded_output, base_model=base_model, save_dir=save_dir)


def get_generate_params(model_lower, chat,
                        stream_output, show_examples,
                        prompt_type, temperature, top_p, top_k, num_beams,
                        max_new_tokens, min_new_tokens, early_stopping, max_time,
                        repetition_penalty, num_return_sequences,
                        do_sample):
    use_defaults = False
    use_default_examples = True
    examples = []
    task_info = f"{prompt_type}"
    if model_lower:
        print(f"Using Model {model_lower}", flush=True)
    else:
        print("No model defined yet", flush=True)

    min_new_tokens = min_new_tokens if min_new_tokens is not None else 0
    early_stopping = early_stopping if early_stopping is not None else False
    max_time_defaults = 60 * 3
    max_time = max_time if max_time is not None else max_time_defaults

    if not prompt_type and model_lower in inv_prompt_type_to_model_lower:
        prompt_type = inv_prompt_type_to_model_lower[model_lower]

    # examples at first don't include chat, instruction_nochat, iinput_nochat, added at end
    if show_examples is None:
        if chat:
            show_examples = False
        else:
            show_examples = True

    summarize_example1 = """Jeff: Can I train a ? Transformers model on Amazon SageMaker? 
Philipp: Sure you can use the new Hugging Face Deep Learning Container. 
Jeff: ok.
Jeff: and how can I get started? 
Jeff: where can I find documentation? 
Philipp: ok, ok you can find everything here. https://huggingface.co/blog/the-partnership-amazon-sagemaker-and-hugging-face"""

    if 'bart-large-cnn-samsum' in model_lower or 'flan-t5-base-samsum' in model_lower:
        placeholder_instruction = summarize_example1
        placeholder_input = ""
        use_defaults = True
        use_default_examples = False
        examples += [
            [placeholder_instruction, "", "", stream_output, 'plain', 1.0, 1.0, 50, 1, 128, 0, False, max_time_defaults,
             1.0, 1,
             False]]
        task_info = "Summarization"
    elif 't5-' in model_lower or 't5' == model_lower or 'flan-' in model_lower:
        placeholder_instruction = "The square root of x is the cube root of y. What is y to the power of 2, if x = 4?"
        placeholder_input = ""
        use_defaults = True
        use_default_examples = True
        task_info = "Multi-Task: Q/A, translation, Chain-of-Thought, Logical Reasoning, Summarization, etc.  Best to use task prefix as trained on, e.g. `translate English to German: ` (space after colon)"
    elif 'mbart-' in model_lower:
        placeholder_instruction = "The girl has long hair."
        placeholder_input = ""
        use_defaults = True
        use_default_examples = False
        examples += [
            [placeholder_instruction, "", "", stream_output, 'plain', 1.0, 1.0, 50, 1, 128, 0, False, max_time_defaults,
             1.0, 1,
             False]]
    elif 'gpt2' in model_lower:
        placeholder_instruction = "The sky is"
        placeholder_input = ""
        prompt_type = prompt_type or 'plain'
        use_default_examples = True  # some will be odd "continuations" but can be ok
        examples += [
            [placeholder_instruction, "", "", stream_output, 'plain', 1.0, 1.0, 50, 1, 128, 0, False, max_time_defaults,
             1.0, 1,
             False]]
        task_info = "Auto-complete phrase, code, etc."
        use_defaults = True
    else:
        if chat:
            placeholder_instruction = "Enter a question or imperative."
        else:
            placeholder_instruction = "Give detailed answer for whether Einstein or Newton is smarter."
        placeholder_input = ""
        if model_lower:
            prompt_type = prompt_type or 'human_bot'
        else:
            prompt_type = ''
        examples += [[summarize_example1, 'Summarize' if prompt_type not in ['plain', 'instruct_simple'] else '', "",
                      stream_output, prompt_type or 'plain', 0.1, 0.75, 40, 4, 256, 0, False, max_time_defaults, 1.0, 1,
                      False]]
        task_info = "No task"
        if prompt_type == 'instruct':
            task_info = "Answer question or follow imperative as instruction with optionally input."
        elif prompt_type == 'plain':
            task_info = "Auto-complete phrase, code, etc."
        elif prompt_type == 'human_bot':
            if chat:
                task_info = "Chat (Shift-Enter to give question/imperative, input concatenated with instruction)"
            else:
                task_info = "Ask question/imperative (input concatenated with instruction)"

    # revert to plain if still nothing
    prompt_type = prompt_type or 'plain'
    if use_defaults:
        temperature = 1.0 if temperature is None else temperature
        top_p = 1.0 if top_p is None else top_p
        top_k = 40 if top_k is None else top_k
        num_beams = num_beams or 1
        max_new_tokens = max_new_tokens or 128
        repetition_penalty = repetition_penalty or 1.07
        num_return_sequences = min(num_beams, num_return_sequences or 1)
        do_sample = False if do_sample is None else do_sample
    else:
        temperature = 0.1 if temperature is None else temperature
        top_p = 0.75 if top_p is None else top_p
        top_k = 40 if top_k is None else top_k
        if chat:
            num_beams = num_beams or 1
        else:
            num_beams = num_beams or 4
        max_new_tokens = max_new_tokens or 256
        repetition_penalty = repetition_penalty or 1.07
        num_return_sequences = min(num_beams, num_return_sequences or 1)
        do_sample = False if do_sample is None else do_sample
    # doesn't include chat, instruction_nochat, iinput_nochat, added later
    params_list = ["", stream_output, prompt_type, temperature, top_p, top_k, num_beams, max_new_tokens, min_new_tokens,
                   early_stopping, max_time, repetition_penalty, num_return_sequences, do_sample]

    if use_default_examples:
        examples += [
            ["Translate English to French", "Good morning"] + params_list,
            ["Give detailed answer for whether Einstein or Newton is smarter.", ''] + params_list,
            ["Explain in detailed list, all the best practices for coding in python.", ''] + params_list,
            [
                "Create a markdown table with 3 rows for the primary colors, and 2 columns, with color name and hex codes.",
                ''] + params_list,
            ['Translate to German:  My name is Arthur', ''] + params_list,
            ["Please answer to the following question. Who is going to be the next Ballon d'or?", ''] + params_list,
            ['Can Geoffrey Hinton have a conversation with George Washington? Give the rationale before answering.',
             ''] + params_list,
            ['Please answer the following question. What is the boiling point of Nitrogen?', ''] + params_list,
            ['Answer the following yes/no question. Can you write a whole Haiku in a single tweet?', ''] + params_list,
            ["Simplify the following expression: (False or False and True). Explain your answer.", ''] + params_list,
            [
                "Premise: At my age you will probably have learnt one lesson. Hypothesis:  It's not certain how many lessons you'll learn by your thirties. Does the premise entail the hypothesis?",
                ''] + params_list,
            ['The square root of x is the cube root of y. What is y to the power of 2, if x = 4?', ''] + params_list,
            [
                'Answer the following question by reasoning step by step.  The cafeteria had 23 apples. If they used 20 for lunch, and bought 6 more, how many apple do they have?',
                ''] + params_list,
            ["""def area_of_rectangle(a: float, b: float):
    \"\"\"Return the area of the rectangle.\"\"\"""", ''] + params_list,
            ["""# a function in native python:
def mean(a):
    return sum(a)/len(a)

# the same function using numpy:
import numpy as np
def mean(a):""", ''] + params_list,
            ["""X = np.random.randn(100, 100)
y = np.random.randint(0, 1, 100)

# fit random forest classifier with 20 estimators""", ''] + params_list,
        ]

    src_lang = "English"
    tgt_lang = "Russian"

    # move to correct position
    for example in examples:
        example += [chat, '', '']
        # adjust examples if non-chat mode
        if not chat:
            example[eval_func_param_names.index('instruction_nochat')] = example[
                eval_func_param_names.index('instruction')]
            example[eval_func_param_names.index('instruction')] = ''

            example[eval_func_param_names.index('iinput_nochat')] = example[eval_func_param_names.index('iinput')]
            example[eval_func_param_names.index('iinput')] = ''

    return placeholder_instruction, placeholder_input, \
           stream_output, show_examples, \
           prompt_type, temperature, top_p, top_k, num_beams, \
           max_new_tokens, min_new_tokens, early_stopping, max_time, \
           repetition_penalty, num_return_sequences, \
           do_sample, \
           src_lang, tgt_lang, \
           examples, \
           task_info


def languages_covered():
    # https://huggingface.co/facebook/mbart-large-50-many-to-many-mmt#languages-covered
    covered = """Arabic (ar_AR), Czech (cs_CZ), German (de_DE), English (en_XX), Spanish (es_XX), Estonian (et_EE), Finnish (fi_FI), French (fr_XX), Gujarati (gu_IN), Hindi (hi_IN), Italian (it_IT), Japanese (ja_XX), Kazakh (kk_KZ), Korean (ko_KR), Lithuanian (lt_LT), Latvian (lv_LV), Burmese (my_MM), Nepali (ne_NP), Dutch (nl_XX), Romanian (ro_RO), Russian (ru_RU), Sinhala (si_LK), Turkish (tr_TR), Vietnamese (vi_VN), Chinese (zh_CN), Afrikaans (af_ZA), Azerbaijani (az_AZ), Bengali (bn_IN), Persian (fa_IR), Hebrew (he_IL), Croatian (hr_HR), Indonesian (id_ID), Georgian (ka_GE), Khmer (km_KH), Macedonian (mk_MK), Malayalam (ml_IN), Mongolian (mn_MN), Marathi (mr_IN), Polish (pl_PL), Pashto (ps_AF), Portuguese (pt_XX), Swedish (sv_SE), Swahili (sw_KE), Tamil (ta_IN), Telugu (te_IN), Thai (th_TH), Tagalog (tl_XX), Ukrainian (uk_UA), Urdu (ur_PK), Xhosa (xh_ZA), Galician (gl_ES), Slovene (sl_SI)"""
    covered = covered.split(', ')
    covered = {x.split(' ')[0]: x.split(' ')[1].replace(')', '').replace('(', '') for x in covered}
    return covered


def test_test_prompt(prompt_type='instruct', data_point=0):
    example_data_point = example_data_points[data_point]
    example_data_point.pop('output', None)
    return generate_prompt(example_data_point, prompt_type, False, False)


if __name__ == "__main__":
    print("""
    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 generate.py --base_model='EleutherAI/gpt-j-6B' --lora_weights=lora-alpaca_6B
    python generate.py --base_model='EleutherAI/gpt-j-6B' --lora_weights='lora-alpaca_6B'
    python generate.py --base_model='EleutherAI/gpt-neox-20b' --lora_weights='lora-alpaca_20B'
    
    # generate without lora weights, no prompt
    python generate.py --base_model='EleutherAI/gpt-neox-20b' --prompt_type='plain'
    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='dai_faq'

    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='dai_faq' --lora_weights='lora_20B_daifaq'
    # OpenChatKit settings:
    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='human_bot --debug=True --num_beams=1 --temperature=0.6 --top_k=40 --top_p=1.0

    python generate.py --base_model='distilgpt2' --prompt_type='plain' --debug=True --num_beams=1 --temperature=0.6 --top_k=40 --top_p=1.0 --share=False
    python generate.py --base_model='t5-large' --prompt_type='simple_instruct'
    python generate.py --base_model='philschmid/bart-large-cnn-samsum'
    python generate.py --base_model='philschmid/flan-t5-base-samsum'
    python generate.py --base_model='facebook/mbart-large-50-many-to-many-mmt'

    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='human_bot' --lora_weights='GPT-NeoXT-Chat-Base-20B.merged.json.8_epochs.57b2892c53df5b8cefac45f84d019cace803ef26.28'

    must have 4*48GB GPU and run without 8bit in order for sharding to work with infer_devices=False
    can also pass --prompt_type='human_bot' and model can somewhat handle instructions without being instruct tuned
    python generate.py --base_model=decapoda-research/llama-65b-hf --load_8bit=False --infer_devices=False --prompt_type='human_bot'

    python generate.py --base_model=h2oai/h2ogpt-oig-oasst1-512-6.9b

    """, flush=True)
    fire.Fire(main)
