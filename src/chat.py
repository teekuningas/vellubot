import os
import openai
import tiktoken

from typing import List, Tuple, Optional
from pprint import pprint


openai.api_key = os.environ.get("OPENAI_API_KEY")
openai.organization = os.environ.get("OPENAI_ORGANIZATION_ID")


def chat(
    history: List[Tuple[str, str]],
    name: str,
) -> List[Tuple[str, str]]:
    buffer_tokens = 64
    wrapper_tokens = 5
    max_tokens = 512

    model = os.environ.get("OPENAI_MODEL") or "gpt-4"

    def count_tokens(text):
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))

    # The instruction prompt
    instruction = f"""
You are an AI chat bot named {name} operating in an IRC chat. Your role is to interact with users, respond to their questions, provide helpful and accurate information, and engage in general conversation. You have access to the history of the chat and should use this context when formulating your responses. Your responses may span multiple lines, and will be reformatted to multiple IRC messages. When formulating the messages, remember that IRC does not have any special formatting such as markdown. You should not prefix the messages with your name.
    """

    # Calculate how many tokens we can use for the conversation history
    tokens_available = max_tokens - count_tokens(instruction) - buffer_tokens

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

    # Create history string
    history_str = "\n".join([": ".join((msg[0], msg[1])) for msg in history])

    # Prepare prompt that ChatCompletion understands
    messages_prompt = []
    messages_prompt.append({"role": "system", "content": instruction})
    for msg in history:
        messages_prompt.append({"role": "user", "content": history_str})

    # And run the query
    response = openai.ChatCompletion.create(
        model=model, messages=messages_prompt, temperature=0
    )

    # Extract the message
    new_history_str = response["choices"][0]["message"]["content"]
    new_history = [(name, msg) for msg in new_history_str.split("\n") if msg]

    return new_history


if __name__ == "__main__":
    history = [
        ("Zairex", "Mitä eroa on nvidian ja amd:n näytönohjaimilla?"),
        ("zups", "Ja mitä yhteistä niillä on?"),
    ]

    pprint(history)

    new_history = chat(history, "vellubot")

    pprint(new_history)
