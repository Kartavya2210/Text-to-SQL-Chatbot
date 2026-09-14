# 🗣️ Text to SQL Chatbot

A natural language interface for SQL databases — ask questions in plain English and get answers instantly, no SQL knowledge required.

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green?logo=fastapi)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LangChain-0.1+-orange)](https://langchain.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## 📚 Table of Contents
- [📖 Project Overview](#-project-overview)
- [🔧 Features](#-features)
- [🛠️ Installation](#️-installation)
- [⚙️ Configuration](#️-configuration)
- [🚀 Usage](#-usage)
- [🗼 Architecture](#-architecture)
- [📊 Evaluation](#-evaluation)
- [📝 Future Work](#-future-work)
- [📄 License](#-license)

---

## 📖 Project Overview

In many organizations, team members need access to data stored in SQL databases but lack the technical skills to write SQL queries. This chatbot bridges that gap — users ask questions in natural language, the app converts them into SQL queries, and returns the results in a human-readable format.

Built with **FastAPI**, **LangChain**, and support for **Google Gemini**, **Groq**, and **Ollama** (local LLMs), the project works with **SQLite**, **PostgreSQL**, and **MySQL** out of the box.

Applicable domains include healthcare, retail, finance, government data, and more.

---

## 🔧 Features

- **Natural Language to SQL**: Converts plain English questions into valid SQL queries using an LLM.
- **Multi-LLM Support**: Works with Google Gemini, Groq (cloud), or Ollama (local/offline).
- **Multi-Database Support**: SQLite, PostgreSQL, MySQL — configurable via `.env`.
- **Generic Mode**: Auto-introspects any SQLite database schema — no hardcoded table assumptions.
- **Streaming Responses**: Real-time response streaming via FastAPI `StreamingResponse`.
- **Response Naturalizer**: Optional second LLM pass to convert raw SQL results into friendly English.
- **Query Logging**: All queries and responses are logged to a local `query_logs.db`.
- **RAGAS Evaluation**: Notebooks included for evaluating retrieval and generation quality.

---

## 🛠️ Installation

### Prerequisites
- Python 3.10+
- (Optional) [Ollama](https://ollama.com/) for local LLM support

### Steps

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Kartavya2210/Text-to-SQL-Chatbot.git
   cd Text-to-SQL-Chatbot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up your environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your settings (see Configuration section below)
   ```

4. **Run the app**:
   ```bash
   uvicorn app:app --reload
   ```
   Then open `http://localhost:8000` in your browser.

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in the relevant values:

```env
# Database — SQLite (default)
DB_URI=sqlite:///citizen_data.db

# Or connect to PostgreSQL / MySQL:
# DB_HOST=localhost
# DB_PORT=5432
# DB_USER=your_user
# DB_PASSWORD=your_password
# DB_NAME=your_database

# LLM Provider: gemini | groq | ollama
LLM_PROVIDER=ollama

# Google Gemini
GOOGLE_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.0-flash

# Groq
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=gemma2-9b-it

# Ollama (local)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5-coder:7b
```

---

## 🚀 Usage

1. Start the server: `uvicorn app:app --reload`
2. Open `http://localhost:8000`
3. Type a question like: *"How many citizens are from Maharashtra?"*
4. The chatbot converts it to SQL, queries the database, and returns the answer.

You can also explore the auto-generated API docs at `http://localhost:8000/docs`.

---

## 🗼 Architecture

```
User Question (Natural Language)
        │
        ▼
   FastAPI Backend
        │
        ▼
  LangChain LLM Chain  ◄──── Database Schema
        │
        ▼
    SQL Query
        │
        ▼
   SQLite / PostgreSQL / MySQL
        │
        ▼
  Raw Results → (Optional) Response LLM
        │
        ▼
  Natural Language Answer
```

**Components:**
- **Frontend**: Vanilla HTML/CSS/JS served as static files via FastAPI
- **Backend**: FastAPI with async support and streaming
- **LLM Chain**: LangChain with pluggable LLM providers (Gemini / Groq / Ollama)
- **Database**: SQLAlchemy ORM, supports SQLite, PostgreSQL, MySQL
- **Evaluation**: Jupyter notebooks with RAGAS metrics

---

## 📊 Evaluation

Performance can be evaluated using:
- **Accuracy**: Correctness of generated SQL queries vs. expected output
- **Response Time**: Latency from question to answer
- **RAGAS Metrics**: Faithfulness, answer relevancy, context recall (see `Gemini Chatbot (including RAGAS).ipynb`)
- **User Feedback**: Collected through the chat interface

---

## 📝 Future Work

- [ ] Support for more databases (BigQuery, Snowflake)
- [ ] Multi-table join reasoning improvements
- [ ] User authentication and query history
- [ ] Docker deployment setup
- [ ] Fine-tuned SQL generation model

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](./LICENSE) for details.

---

> Built by [Kartavya2210](https://github.com/Kartavya2210) · Contributions and feedback welcome! 🎉
