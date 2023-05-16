def test_chat_context():
    # on h2oai/h2ogpt-oasst1-512-20b
    instruction = """Rephrase in 5 different ways: “Apple a day keeps the doctor away.”"""
    expected_response = """1. “A apple every day will keep you healthy.”
2. “An Apple A Day Keeps The Doctor Away”
3. “One of these apples each and everyday, is all it takes to stay well”
4. “Eat an apple daily for good health!”
5. “If eaten one per day, this fruit can help prevent disease”.

I hope that helps! Let me know if there’s anything else I could do for you today?"""
    instruction2 = """Summarize into single sentence."""
    expected_response2 = """“The more fruits we eat, the healthier.” - Dr. John Yiamouyiannis (American physician)"""

    # NOTE: if something broken, might say something unrelated to first question, e.g.
    unexpected_response2 = """I am an AI language model ..."""

    raise NotImplementedError("MANUAL TEST FOR NOW")
