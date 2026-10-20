"""Connection check only (not part of the app): one OpenAI call with the key from .env. Run: python scripts/testopenai.py"""
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

response = client.responses.create(
    model="gpt-4.1-mini",
    input="Reply with exactly: EgoQuant OpenAI connection successful."
)

print(response.output_text)