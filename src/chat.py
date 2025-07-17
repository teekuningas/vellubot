import logging
import os
import tiktoken

from typing import List, Tuple, Optional
from pprint import pprint

from openai import OpenAI
from openai import AzureOpenAI
from openai.types.chat import ChatCompletionMessageParam


if os.environ.get("OPENAI_API_KEY"):
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        organization=os.environ.get("OPENAI_ORGANIZATION_ID", ""),
        base_url=os.environ.get("OPENAI_BASE_URL", ""),
    )
else:
    client = AzureOpenAI(
        api_key=os.environ.get("AZURE_OPENAI_KEY", ""),
        azure_endpoint=os.environ.get("AZURE_ENDPOINT", ""),
        api_version=os.environ.get("AZURE_API_VERSION", ""),
    )


logger = logging.getLogger("app")


def chat(
    history: List[Tuple[str, str]],
    name: str,
    instruction: Optional[str] = None,
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

    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    def count_tokens(text):
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))

    # The instruction prompt
    if instruction is None:
        instruction = f"""You are {name}, a helpful AI assistant in an IRC chat.

Your main goal is to engage in conversations with the users. Participate naturally, answer questions, and provide information using the chat history for context.

Adapt your language to the language being used in the channel. If people are speaking Finnish, you should respond in Finnish.

Your responses are plain text for IRC, so do not use any special formatting like Markdown.

The chat history shows messages like `username: message`. If a user says `{name}: hello`, they are talking directly to you. When you reply to a specific user, you can start your message with their name, like `sipsu: I'm doing great!`. You don't always have to address a specific user.

Most importantly, never start your own messages with your name, `{name}:`. The IRC client does this for you. Just write your message. For example, instead of writing `{name}: sipsu: I can help`, you should just write `sipsu: I can help`.

Finally, only provide your own response. Do not continue the conversation for other users. Let them speak for themselves.
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
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_content},
    ]

    # And run the query
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=max_tokens_out,
        timeout=30,
    )

    # Extract the message
    new_history_str = response.choices[0].message.content or ""

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
