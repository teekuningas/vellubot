import logging
import os
import openai
import tiktoken

from typing import List, Tuple, Optional
from pprint import pprint


openai.api_key = os.environ.get("OPENAI_API_KEY")
openai.organization = os.environ.get("OPENAI_ORGANIZATION_ID")


logger = logging.getLogger("app")


def chat(
    history: List[Tuple[str, str]],
    name: str,
) -> List[Tuple[str, str]]:
    buffer_tokens = 64
    wrapper_tokens = 5

    max_tokens_in = 2048
    if os.environ.get("OPENAI_MAX_TOKENS_IN"):
        openai_max_tokens_in = os.environ.get("OPENAI_MAX_TOKENS_IN")
        if openai_max_tokens_in is not None:
            try:
                max_tokens_in = int(openai_max_tokens_in)
            except ValueError as exc:
                pass

    max_tokens_out = 256
    if os.environ.get("OPENAI_MAX_TOKENS_OUT"):
        openai_max_tokens_out = os.environ.get("OPENAI_MAX_TOKENS_OUT")
        if openai_max_tokens_out is not None:
            try:
                max_tokens_out = int(openai_max_tokens_out)
            except ValueError as exc:
                pass

    model = os.environ.get("OPENAI_MODEL") or "gpt-3.5-turbo"

    def count_tokens(text):
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))

    # The instruction prompt
    instruction = f"""
You are an AI chat bot named {name} operating in an IRC chat. Your role is to interact with users, respond to their questions, provide helpful and accurate information, and engage in general conversation. You have access to the history of the chat, including your own earlier messages, and should use this context when formulating your responses. Your responses may span multiple lines, and will be reformatted to multiple IRC messages. When formulating the messages, remember that IRC does not have any special formatting such as markdown. You should not prefix the messages with your name.
    """

    # Calculate how many tokens we can use for the conversation history
    tokens_available = max_tokens_in - count_tokens(instruction) - buffer_tokens

    # Truncate conversation history if necessary
    history_str = "\n".join([": ".join((msg[0], msg[1])) for msg in history])
    if count_tokens(history_str) + wrapper_tokens * len(history) > tokens_available:
        while history:
            history_str = "\n".join([": ".join((msg[0], msg[1])) for msg in history])
            if (
                count_tokens(history_str) + wrapper_tokens * len(history)
                <= tokens_available
            ):
                break
            else:
                history.pop(0)

    # Log to debug log
    logger.debug("History: ")
    logger.debug("\n" + history_str)

    # Prepare prompt that ChatCompletion understands

    user_content = "\n".join([f"{elem[0]}: {elem[1]}" for elem in history])
    messages_prompt = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_content},
    ]

    # And run the query
    response = openai.ChatCompletion.create(
        model=model,
        messages=messages_prompt,
        temperature=0,
        max_tokens=max_tokens_out,
        request_timeout=30,
    )

    # Extract the message
    new_history_str = response["choices"][0]["message"]["content"]

    # Log it debug log
    logger.debug("OpenAI Response:")
    logger.debug("\n" + new_history_str)

    # Parse from string
    new_history: List[Tuple[str, str]] = []
    for line in new_history_str.split("\n"):
        if not line:
            continue

        # remove name from beginning if present..
        if line.startswith(f"{name}: "):
            msg = line.split(f"{name}: ")[1]
        else:
            msg = line

        assert isinstance(msg, str)

        new_history.append((name, msg))

    return new_history


if __name__ == "__main__":
    history = [
        ("Zairex", "Mitä eroa on nvidian ja amd:n näytönohjaimilla?"),
        ("zups", "Ja mitä yhteistä niillä on?"),
    ]
    new_history = chat(history, "vellubot")
