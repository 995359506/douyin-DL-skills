# AGENTS.md - Coding Guidelines for Douyin Downloader

## Build & Test Commands

### Python Backend
```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
python tests/run_all_tests.py

# Run single test file
python -m pytest tests/unit/test_smoke.py -v
python -m pytest tests/integration/test_cli.py -v
python -m pytest tests/integration/test_api.py -v

# Run single test function
python -m pytest tests/unit/test_smoke.py::test_import -v

# Start backend server
python -m skills.douyin_downloader.web --port 8080

# CLI usage
python -m skills.douyin_downloader.cli --help
python -m skills.douyin_downloader.cli mix <mix_id> --name "Name"
python -m skills.douyin_downloader.cli video <url> --wait
```

### Frontend (Vue.js)
```bash
cd frontend

# Install dependencies
npm install

# Development server
npm run dev

# Build for production
npm run build
```

## Code Style Guidelines

### Python Imports
- **Use try/except pattern for relative imports** to support both package and direct execution:
```python
try:
    from .logger import get_logger
    from .db import TaskDB
except ImportError:
    from logger import get_logger
    from db import TaskDB
```
- Standard library imports first, then third-party, then local
- Use `from typing import ...` for type hints

### Type Hints
- Always use type hints for function parameters and return types
- Use `Optional[Type]` for nullable values
- Use `Dict[str, Any]` and `List[Type]` from typing module
- Example: `async def download(self, url: str) -> Dict[str, Any]:`

### Naming Conventions
- **Classes**: PascalCase (e.g., `DouyinDownloadSkill`, `TaskManager`)
- **Functions/Methods**: snake_case (e.g., `download_video`, `get_cookie`)
- **Variables**: snake_case (e.g., `task_id`, `mix_name`)
- **Constants**: UPPER_CASE (e.g., `COOKIE_FILE`, `DEFAULT_TIMEOUT`)
- **Private methods**: prefix with underscore (e.g., `_build_kwargs`, `_get_handler`)

### Async/Await Patterns
- This is an async codebase - use `async def` for I/O operations
- Use `await` when calling async functions
- For sync operations that block, use `asyncio.get_event_loop().run_in_executor()`

### Error Handling
- Use try/except with specific exceptions
- Log errors using the project's logger: `logger.error(f"Message: {e}")`
- Return error info in dictionaries: `{"success": False, "message": str(e)}`
- For FastAPI endpoints, use `HTTPException` with appropriate status codes

### Logging
- Use the project's logger: `from .logger import get_logger; logger = get_logger()`
- Log levels: `logger.info()`, `logger.error()`, `logger.warning()`, `logger.debug()`
- Include context: `logger.info("Download complete", extra={"task_id": task_id})`

### Documentation
- Use Chinese for docstrings and comments (project convention)
- Follow Google-style docstrings:
```python
def function(arg: str) -> Dict[str, Any]:
    """
    Brief description.
    
    Args:
        arg: Description
        
    Returns:
        Description of return value
    """
```

### Database & Models
- Use `@dataclass` for data models
- Use `aiosqlite` for async database operations
- Store timestamps in ISO format strings

### F2 Framework Integration
- Import from F2: `from f2.apps.douyin.handler import DouyinHandler`
- Use `ConfigManager` for configuration
- Follow F2's patterns for handlers and downloaders

### Configuration
- Default config path: `./my_douyin.yaml`
- Use F2's `ConfigManager` to load configs
- Configs are passed as kwargs to F2 components

### Testing
- Use pytest
- Place tests in `tests/` directory
- Use descriptive test function names: `test_download_video_success()`

### Project Structure
```
skills/douyin_downloader/    # Main skill package
├── core.py                  # Main download logic
├── web.py                   # FastAPI web server
├── cli.py                   # Command line interface
├── task_manager_v3.py       # Async task management
├── db.py                    # Database models & operations
├── cookie_manager.py        # Cookie handling
├── logger.py                # Logging setup
├── rate_limiter.py          # Rate limiting
└── SKILL.md                 # Skill documentation
```

## Common Patterns

### Creating a new endpoint (web.py)
```python
@app.post("/api/endpoint", response_model=ApiResponse)
async def my_endpoint(request: MyRequestModel):
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    try:
        result = await skill.some_method()
        return ApiResponse(success=True, data=result, message="成功")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### Adding a CLI command (cli.py)
```python
async def cmd_my_command(self, arg: str):
    """Command description"""
    try:
        result = await self.manager.do_something(arg)
        print_json({"success": True, "data": result})
    except Exception as e:
        print_json({"success": False, "error": str(e)})
        sys.exit(1)
```

## Important Notes
- Always handle the case when `skill is None` in web endpoints
- Use `[OK]` `[FAIL]` instead of `✓` `✗` (Windows encoding issues)
- Keep F2's `enable_bark: false` in `conf/conf.yaml` to avoid timeouts
- Cookie file: `douyin_cookie.txt` (auto-generated)
- Database files: `douyin_downloader.db`, `douyin_videos.db`
