# Clause — Your lease, in plain English

The first document that has real power over your life is your lease — and 44 million Americans sign one every year without understanding it. Clause changes that, privately, by voice, on your device.

## The Problem

44 million US renter households sign leases they don't understand. When something goes wrong — unauthorized pets, noise violations, sketchy new clauses, unauthorized guests — they have no idea what their rights are. They'd never upload their lease to a random cloud app for privacy reasons. And they're too stressed to read through dense legal text.

## The Solution

Upload your lease, tap the mic, ask in plain English. Get grounded answers from your actual clauses, spoken back to you by voice. Toggle between **Private** (on-device Gemma 4 via Cactus — nothing leaves your device) and **Cloud** (Gemini — higher accuracy) based on your privacy needs.

## Why Not ChatGPT?

Your lease never leaves your device in Private mode. Voice-native means you can use it while stressed, walking to your car, standing outside your landlord's office. It knows **your** document, not generic legal info.

## Tech Stack

- **Backend:** Python + FastAPI
- **On-device inference:** Gemma 4 (E2B) via Cactus SDK — hybrid routing
- **Cloud fallback:** Gemini 3 Flash via Google AI Studio
- **Voice input:** Web Speech API
- **Voice output:** ElevenLabs TTS (Rachel) via official Python SDK, with browser speech synthesis as fallback
- **PDF parsing:** pypdf + RAG retrieval
- **Frontend:** Vanilla JS/CSS, single page

## Demo Scenario

*My roommate brought a cat into our 4-person apartment 2 months ago against the pet policy. Am I liable? What can my landlord do? How do I handle this conversation?* — answered in seconds from the actual lease clauses, spoken back by voice.

## Market

- 44M renter households in the US
- **Expansion:** employment contracts, NDAs, insurance policies, medical consent forms
- **B2B:** property managers, law firms (data can't leave device due to liability)

## Built by

**Lena Munad** — Solo technical founder of Moneyhubb (AI financial assistant, live on Apple store), B.S. Data Science at San Jose State University. Built Clause in two days at the YC x Google DeepMind x Cactus Gemma 4 Voice Agents Hackathon.

## How to Run

**Prerequisites:** macOS (recommended for Cactus + on-device), Python 3.10+.

### 1. Set up Cactus and Gemma 4 weights

```bash
git clone https://github.com/cactus-compute/cactus
cd cactus && source ./setup && cd ..
cactus build --python
cactus download google/gemma-4-E2B-it --reconvert
cactus auth
```

`cactus auth` will prompt for your Cactus API key from the [Cactus dashboard](https://cactuscompute.com/dashboard/api-keys).

### 2. Clone this repo and install Clause

```bash
git clone https://github.com/IceyGirl424/voice-agents-hack
cd voice-agents-hack/clause
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Create a `.env` file in the `clause` folder

Copy the variables below and fill in your keys. Paths to Cactus should match your local clone (the `weights` directory is created under the Cactus repo when you run `cactus download`).

```env
GEMINI_API_KEY=your-gemini-key
GEMINI_MODEL=gemini-3-flash-preview

# On-device (Private mode) — point at your converted weights folder
CACTUS_MODEL_PATH=/path/to/cactus/weights/gemma-4-e2b-it
CACTUS_PYTHON_SRC=/path/to/cactus/python/src

# Optional: natural TTS for spoken answers (falls back to browser TTS if unset)
ELEVEN_API_KEY=your-elevenlabs-key
```

- **Gemini key:** [Google AI Studio](https://aistudio.google.com/api-keys)  
- **Cactus:** `CACTUS_MODEL_PATH` is the directory containing `config.txt` and `*.weights` shards (not a single `.gguf` file).  
- **ElevenLabs:** used by `POST /api/speak` for high-quality voice; optional.

### 4. Start the server

```bash
cd voice-agents-hack/clause
source .venv/bin/activate
uvicorn app:app --reload --host 127.0.0.1 --port 8768
```

### 5. Open the app

[http://127.0.0.1:8768](http://127.0.0.1:8768) — upload a lease PDF, choose **Private** or **Cloud**, tap the mic, and ask your question.

---

*This repository’s hackathon template context and Cactus setup notes live in `assets/` and history; the **Clause** app code is in the `clause/` directory.*
