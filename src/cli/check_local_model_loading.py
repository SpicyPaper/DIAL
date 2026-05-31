from src.env_config import env_bool, env_int, load_project_env, require_env
from transformers import AutoModelForCausalLM, AutoTokenizer

load_project_env()

model_id = require_env("LOCAL_MODEL_ID")
max_new_tokens = env_int("LOCAL_MAX_NEW_TOKENS")
enable_thinking = env_bool("LOCAL_ENABLE_THINKING")

tokenizer = AutoTokenizer.from_pretrained(model_id)
model = AutoModelForCausalLM.from_pretrained(
    model_id,
    torch_dtype="auto",
    device_map="auto",
)

messages = [{"role": "user", "content": "Explain DHT in two short sentences."}]

text = tokenizer.apply_chat_template(
    messages,
    tokenize=False,
    add_generation_prompt=True,
    enable_thinking=enable_thinking,
)

inputs = tokenizer([text], return_tensors="pt").to(model.device)

outputs = model.generate(
    **inputs,
    max_new_tokens=max_new_tokens,
    do_sample=True,
    temperature=0.7,
    top_p=0.8,
    top_k=20,
)

generated_ids = outputs[0][len(inputs.input_ids[0]) :]
answer = tokenizer.decode(generated_ids, skip_special_tokens=True)

print(answer.strip())
