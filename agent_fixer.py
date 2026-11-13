import os
import re
from google import genai

# 1. Setup the modern Gemini Client
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 2. Read the broken file and the error log
try:
    with open("error.log", "r") as f:
        error_log = f.read()
    with open("Dockerfile", "r") as f:
        dockerfile = f.read()
except FileNotFoundError:
    print("Error: Missing error.log or Dockerfile. Exiting.")
    exit(1)

# 3. Prompt the AI
prompt = f"""You are an AIOps Agent. The attached Dockerfile failed to build. 
Error Log: {error_log}
Broken Dockerfile: {dockerfile}
Output ONLY the corrected Dockerfile code. Do not include any markdown formatting, backticks, or explanations."""

print("Sending failure context to Gemini...")

# 4. Use the correct, currently supported model
response = client.models.generate_content(
    model='gemini-3.8-flash',
    contents=prompt
)
fixed_code = response.text

# 5. Clean the response (strip markdown backticks)
match = re.search(r'```(?:[a-z]*\n)?(.*?)```', fixed_code, re.DOTALL)
if match:
    fixed_code = match.group(1)

# 6. Overwrite the broken Dockerfile
with open("Dockerfile", "w") as f:
    f.write(fixed_code.strip() + "\n")
    
print("Dockerfile successfully patched by AI.")