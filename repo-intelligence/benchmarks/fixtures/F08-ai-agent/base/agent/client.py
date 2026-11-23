import os

import anthropic
import yaml

CONFIG = yaml.safe_load(open("config/models.yaml"))
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def ask(question: str, tools: list[dict]) -> str:
    system = open("prompts/system.md").read()
    response = client.messages.create(model=CONFIG["default_model"], max_tokens=1024, system=system, tools=tools, messages=[{"role": "user", "content": question}])
    return response.content[0].text
