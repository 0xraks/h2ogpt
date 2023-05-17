"""
Client test.

Run server:

python generate.py  --base_model=h2oai/h2ogpt-oig-oasst1-512-6.9b

NOTE: For private models, add --use-auth_token=True

NOTE: --infer_devices=True (default) must be used for multi-GPU in case see failures with cuda:x cuda:y mismatches.
Currently, this will force model to be on a single GPU.

Then run this client as:

python client_test.py



For HF spaces:

HOST="https://h2oai-h2ogpt-chatbot.hf.space" python client_test.py

Result:

Loaded as API: https://h2oai-h2ogpt-chatbot.hf.space ✔
{'instruction_nochat': 'Who are you?', 'iinput_nochat': '', 'response': 'I am h2oGPT, a large language model developed by LAION.'}


For demo:

HOST="https://gpt.h2o.ai" python client_test.py

Result:

Loaded as API: https://gpt.h2o.ai ✔
{'instruction_nochat': 'Who are you?', 'iinput_nochat': '', 'response': 'I am h2oGPT, a chatbot created by LAION.'}

"""

debug = False

import os
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'


def get_client():
    from gradio_client import Client

    client = Client(os.getenv('HOST', "http://localhost:7860"))
    if debug:
        print(client.view_api(all_endpoints=True))
    return client


def test_client_basic():
    return run_client_basic(instruction_nochat='Who are you?', prompt_type='human_bot')


def run_client_basic(instruction_nochat, prompt_type):
    instruction = ''  # only for chat=True
    iinput = ''  # only for chat=True
    context = ''
    # streaming output is supported, loops over and outputs each generation in streaming mode
    # but leave stream_output=False for simple input/output mode
    stream_output = False
    temperature = 0.1
    top_p = 0.75
    top_k = 40
    num_beams = 1
    max_new_tokens = 50
    min_new_tokens = 0
    early_stopping = False
    max_time = 20
    repetition_penalty = 1.0
    num_return_sequences = 1
    do_sample = True
    # only these 2 below used if pass chat=False
    chat = False
    iinput_nochat = ''
    langchain_mode = 'Disabled'

    args = [instruction,
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
            langchain_mode,
            ]
    api_name = '/submit_nochat'
    client = get_client()
    res = client.predict(
        *tuple(args),
        api_name=api_name,
    )
    res_dict = dict(instruction_nochat=instruction_nochat, iinput_nochat=iinput_nochat, response=md_to_text(res))
    print(res_dict)
    return res_dict


import markdown  # pip install markdown
from bs4 import BeautifulSoup  # pip install beautifulsoup4


def md_to_text(md):
    assert md is not None, "Markdown is None"
    html = markdown.markdown(md)
    soup = BeautifulSoup(html, features='html.parser')
    return soup.get_text()


if __name__ == '__main__':
    test_client_basic()
