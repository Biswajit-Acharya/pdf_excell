import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

try:
    models = client.models.list()
    for m in models:
        print(m.name)
except Exception as e:
    print(e)
