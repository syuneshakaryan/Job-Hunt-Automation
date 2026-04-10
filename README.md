# Job-Haunt-Automation

A fully automated job hunting pipeline that crawls company career pages, evaluates job fits using AI, generates tailored resumes, and applies to jobs via ATS systems or contact forms. Built with Python, Playwright, Ollama/Groq, Prefect, and Telegram integration.

## Features

- **Automated Job Discovery**: Crawls company domains to find career pages and extract job postings.
- **AI-Powered Job Evaluation**: Uses local Ollama or Groq API to score job fits based on your resume and preferences.
- **Tailored Resume Generation**: Creates ATS-friendly PDF resumes customized for each high-scoring job.
- **Automated Application**: Fills out ATS forms or submits contact forms with your information.
- **Telegram Notifications**: Receives job alerts with options to auto-apply, skip, or view details.
- **Orchestration**: Uses Prefect for workflow management and scheduling.
- **Local & Private**: Runs entirely on your machine with no data sent to external servers (except LLM APIs if used).

## Project Structure

### Core Files

- **`main.py`**: The main orchestration script using Prefect. Defines the workflow tasks and handles scheduling. Run this to start the pipeline.
- **`config.py`**: Configuration file with API keys, personal info, and settings. **Not included in repo for security; create it locally with placeholders filled in.**
- **`requirements.txt`**: Python dependencies. Install with `pip install -r requirements.txt`.
- **`database.py`**: Manages the SQLite database for storing companies, jobs, and evaluations.
- **`crawler.py`**: Handles web scraping: loads company homepages, finds career pages, and extracts job postings using Playwright.
- **`evaluator.py`**: Uses AI (Ollama or Groq) to evaluate job descriptions and assign fit scores.
- **`resume_builder.py`**: Generates customized resumes in PDF format using Jinja2 templates and WeasyPrint.
- **`telegram_bot.py`**: Manages Telegram notifications and handles user interactions (apply, skip, view JD).
- **`ats_apply.py`**: Automates job applications by filling ATS forms or contact forms.

### Data & Assets

- **`data/company_domains.csv`**: CSV file with company domains to crawl. Format: `domain,status` (e.g., `example.com,pending`).
- **`data/master_resume.json`**: Your full resume data in JSON format, used as the base for generating tailored resumes.
- **`templates/resume.html.j2`**: Jinja2 HTML template for resume generation.
- **`output/resumes/`**: Directory where generated PDF resumes are saved.
- **`output/screenshots/`**: Screenshots from application attempts.
- **`logs/`**: Log files for debugging and monitoring.

## Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/syuneshakaryan/Job-Hunt-Automation.git
cd Job-Hunt-Automation
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium  # Install browser for scraping
```

### 3. Set Up Local LLM (Ollama)

Download and install Ollama from [ollama.com](https://ollama.com/download).

Pull the required model:
```bash
ollama pull llama3.1:8b
```

Start the Ollama server:
```bash
ollama serve  # Runs on http://localhost:11434
```

### 4. Create Configuration File

Create `config.py` in the root directory with the following structure (copy from the template below and fill in your real values):

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = "YOUR_TELEGRAM_BOT_TOKEN"  # Get from @BotFather
    telegram_chat_id: str = "YOUR_TELEGRAM_CHAT_ID"      # Your personal chat ID

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    llm_backend: str = "ollama"  # or "groq"

    groq_api_key: str = "YOUR_GROQ_API_KEY"  # If using Groq instead of Ollama
    groq_model: str = "llama-3.3-70b-versatile"

    # Your personal info for ATS form-filling
    your_full_name: str = "YOUR_FULL_NAME"
    your_email: str = "YOUR_EMAIL"
    your_phone: str = "YOUR_PHONE"
    your_linkedin: str = "YOUR_LINKEDIN_URL"
    your_github: str = "YOUR_GITHUB_URL"
    your_location: str = "YOUR_LOCATION"

    # Pipeline tuning
    batch_size: int = 25
    fit_score_threshold: int = 75
    crawl_delay_seconds: float = 2.0
    page_timeout_ms: int = 15000

    # Paths (derived, not from .env)
    @property
    def base_dir(self) -> Path:
        return Path(__file__).parent

    @property
    def db_path(self) -> Path:
        return self.base_dir / "data" / "job_hunter.db"

    @property
    def resumes_dir(self) -> Path:
        return self.base_dir / "output" / "resumes"

    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"

settings = Settings()
```

- **Telegram Setup**: Create a bot via @BotFather on Telegram, get the token. Find your chat ID by messaging the bot and checking updates.
- **API Keys**: Keep them secret; use environment variables if possible.
- **Personal Info**: Used for auto-filling applications.

### 5. Prepare Data Files

- **`data/master_resume.json`**: Create a JSON file with your resume data. Example structure:
  ```json
  {
    "name": "Your Name",
    "contact": {...},
    "experience": [
      {
        "company": "Company Name",
        "title": "Job Title",
        "bullets": ["Bullet point 1", "Bullet point 2"]
      }
    ],
    "skills": ["Skill1", "Skill2"],
    ...
  }
  ```
- **`data/company_domains.csv`**: Add companies to target, e.g.:
  ```
  domain,status
  example.com,pending
  another.com,pending
  ```

### 6. Initialize Database

Run the pipeline with the `--load-csv` flag to seed the database:
```bash
python main.py --load-csv
```

## How to Run

### Basic Run (One Batch)

```bash
python main.py
```

This processes a batch of companies, crawls jobs, evaluates them, generates resumes for high-scoring jobs, and sends Telegram notifications.

### Scheduled Runs

Start Prefect server for UI and scheduling:
```bash
prefect server start
```

Deploy the flow for daily runs at 8 AM:
```bash
prefect deploy main.py:job_hunter_flow
```

In the Prefect UI (http://127.0.0.1:4200), set up a cron schedule.

### Custom Batch Size

```bash
python main.py --batch-size 50
```

### Telegram Bot

The bot runs automatically with the pipeline. It sends notifications and handles button presses for applying to jobs.

## Pipeline Flow

1. **Load Domains**: Load pending companies from DB.
2. **Crawl Domains**: Visit company sites, find career pages, extract job links.
3. **Extract Jobs**: Scrape job descriptions from postings.
4. **Evaluate Jobs**: Use AI to score fit (0-100) based on your resume.
5. **Generate Resumes**: For jobs >= threshold, create tailored PDFs.
6. **Notify**: Send Telegram alert with resume attached.
7. **Apply (Optional)**: User can trigger auto-apply via Telegram buttons.

## Troubleshooting

- **Ollama Issues**: Ensure Ollama is running and model is pulled.
- **Playwright Errors**: Run `playwright install chromium`.
- **Telegram Not Working**: Check bot token and chat ID.
- **DB Errors**: Delete `data/job_hunter.db` and re-run with `--load-csv`.
- **Secrets Detected**: Ensure `config.py` has placeholders, not real keys.

## Contributing

Feel free to open issues or PRs. Keep security in mind — no secrets in commits.

## License

MIT License.