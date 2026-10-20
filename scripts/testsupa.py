"""Connection check only (not part of the app): connects to Supabase with the URL and key from .env. Run: python scripts/testsupa.py"""
import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

response = supabase.table("connection_test").select("*").limit(1).execute()

print("Supabase connection successful!")
print(response.data)