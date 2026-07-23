# What this assistant can do

This is your operator copilot. It can both **answer questions about your data**
and **take actions** for you. Ask in plain language.

- **Investigate** a person/org across public records ("investigate Acme Corp"),
  and **check run status** ("is that done?").
- **Query the knowledge graph** — entities, facts, events, references, libraries
  ("what do we know about Acme?", "facts about Roswell", "recent facts").
- **Operate** — create a case, a detection rule, a geofence, a knowledge node or
  a data connector; enrich a location; correlate signals; generate an
  intelligence brief; export signals/cases/contacts; navigate the app.
- **Explain the product** — how libraries, federation, the map, and features
  work — and **walk you through setup** (what a feature needs, where to register,
  and a link to the right Settings page).

The assistant is grounded: answers about your data come from real records (with
corroboration counts), and how-to answers come from the product docs — it does
not make things up.

# Setting up features / getting API keys

Ask "how do I set up satellite imagery" or "how do I get an SEC/FEC key" and the
assistant explains what's needed, gives the exact registration link and steps,
and points you to the right Settings page. You paste the key yourself — the
assistant never stores credentials.

Common ones:
- **Satellite imagery (Sentinel-2):** free Sentinel Hub instance ID →
  Settings → Satellite Imagery.
- **Investigation extras (FEC/Congress/FDA):** free DATA_GOV_API_KEY.
- **AI provider:** local Ollama (no key) or a cloud key → Settings → AI.

# Provider / privacy

The AI runs on whatever provider you configure in Settings → AI. Choose local
Ollama to keep everything on your hardware, or a cloud provider (OpenAI,
Anthropic, Gemini, etc.) with your own key.
