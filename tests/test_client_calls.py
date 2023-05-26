import pytest

from tests.utils import wrap_test_forked


@wrap_test_forked
def test_client1():
    import os, sys
    os.environ['TEST_LANGCHAIN_IMPORT'] = "1"
    sys.modules.pop('gpt_langchain', None)
    sys.modules.pop('langchain', None)

    from generate import main
    main(base_model='h2oai/h2ogpt-oig-oasst1-512-6_9b', prompt_type='human_bot', chat=False,
         stream_output=False, gradio=True, num_beams=1, block_gradio_exit=False)

    from client_test import test_client_basic
    res_dict = test_client_basic()
    assert res_dict['prompt'] == 'Who are you?'
    assert res_dict['iinput'] == ''
    assert 'I am h2oGPT' in res_dict['response'] or "I'm h2oGPT" in res_dict['response'] or 'I’m h2oGPT' in res_dict[
        'response']


@wrap_test_forked
def test_client_chat_nostream():
    res_dict = run_client_chat(stream_output=False)
    assert 'I am h2oGPT' in res_dict['response'] or "I'm h2oGPT" in res_dict['response'] or 'I’m h2oGPT' in res_dict[
        'response']


@wrap_test_forked
def test_client_chat_nostream_gpt4all():
    res_dict = run_client_chat(stream_output=False, base_model='gptj', prompt_type='plain')
    assert 'I am a computer program designed to assist' in res_dict['response']


def run_client_chat(prompt='Who are you?', stream_output=False, max_new_tokens=256,
                    base_model='h2oai/h2ogpt-oig-oasst1-512-6_9b', prompt_type='human_bot',
                    langchain_mode='Disabled', user_path=None,
                    visible_langchain_modes=['UserData', 'MyData']):
    import os, sys
    if langchain_mode == 'Disabled':
        os.environ['TEST_LANGCHAIN_IMPORT'] = "1"
        sys.modules.pop('gpt_langchain', None)
        sys.modules.pop('langchain', None)

    from generate import main
    main(base_model=base_model, prompt_type=prompt_type, chat=True,
         stream_output=stream_output, gradio=True, num_beams=1, block_gradio_exit=False,
         max_new_tokens=max_new_tokens,
         langchain_mode=langchain_mode, user_path=user_path,
         visible_langchain_modes=visible_langchain_modes)

    from client_test import run_client_chat
    res_dict = run_client_chat(prompt=prompt, prompt_type='human_bot', stream_output=stream_output,
                               max_new_tokens=max_new_tokens, langchain_mode=langchain_mode)
    assert res_dict['prompt'] == prompt
    assert res_dict['iinput'] == ''
    return res_dict


@wrap_test_forked
def test_client_chat_stream():
    run_client_chat(stream_output=True)


@wrap_test_forked
def test_client_chat_stream_langchain():
    import os
    import shutil

    user_path = 'user_path_test'
    if os.path.isdir(user_path):
        shutil.rmtree(user_path)
    os.makedirs(user_path)
    db_dir = "db_dir_UserData"
    if os.path.isdir(db_dir):
        shutil.rmtree(db_dir)
    shutil.copy('data/pexels-evg-kowalievska-1170986_small.jpg', user_path)
    shutil.copy('README.md', user_path)
    shutil.copy('FAQ.md', user_path)
    prompt = "What is h2oGPT?"
    res_dict = run_client_chat(prompt=prompt, stream_output=True, langchain_mode="UserData", user_path=user_path,
                               visible_langchain_modes=['UserData', 'MyData'])
    # below wouldn't occur if didn't use LangChain with README.md,
    # raw LLM tends to ramble about H2O.ai and what it does regardless of question.
    assert 'h2oGPT is a large language model' in res_dict['response']


@wrap_test_forked
def test_client_chat_stream_long():
    prompt = 'Tell a very long story about cute birds for kids.'
    res_dict = run_client_chat(prompt=prompt, stream_output=True, max_new_tokens=1024)
    assert 'Once upon a time' in res_dict['response']


@pytest.mark.skip(reason="Local file required")
@wrap_test_forked
def test_client_long():
    import os, sys
    os.environ['TEST_LANGCHAIN_IMPORT'] = "1"
    sys.modules.pop('gpt_langchain', None)
    sys.modules.pop('langchain', None)

    from generate import main
    main(base_model='mosaicml/mpt-7b-storywriter', prompt_type='plain', chat=False,
         stream_output=False, gradio=True, num_beams=1, block_gradio_exit=False)

    with open("/home/jon/Downloads/Gatsby_PDF_FullText.txt") as f:
        prompt = f.readlines()

    from client_test import run_client_nochat
    res_dict = run_client_nochat(prompt=prompt, prompt_type='plain', max_new_tokens=86000)
    print(res_dict['response'])
