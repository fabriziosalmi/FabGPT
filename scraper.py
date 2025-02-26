"""GitHub Python Repo Scraper module."""
# Standard library imports
import argparse
import base64
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

# Third-party imports
import requests
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

# Initialize Rich console
console = Console()

# GitHub API base URL
GITHUB_API_URL = "https://api.github.com"
MAX_RETRIES = 3
RETRY_DELAY = 5

# Setup logging
logging.basicConfig(
    filename="scraper.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


class GitHubAPIError(Exception):
    """Custom exception for GitHub API errors."""

    pass


def make_github_request(url, headers, params=None, method="GET", data=None):
    """Makes a request to the GitHub API, with basic error handling."""
    for attempt in range(MAX_RETRIES):
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, params=params)
            elif method == "POST":
                response = requests.post(url, headers=headers, data=data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                logging.error(
                    f"Failed to make request to {url} after {MAX_RETRIES} attempts: {e}"
                )
                raise GitHubAPIError(
                    f"Failed to make request to {url} after {MAX_RETRIES} attempts: {e}"
                ) from e
            wait_time = RETRY_DELAY * (2**attempt)
            console.print(
                f"[yellow]Request failed: {e}. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})[/yellow]"
            )
            logging.warning(
                f"Request failed: {e}. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(wait_time)

    return None


def get_rate_limit_status(token):
    """Fetches and returns the current rate limit status."""
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": "GitHub-Repo-Scraper/1.0",
    }
    try:
        data = make_github_request(
            f"{GITHUB_API_URL}/rate_limit", headers=headers
        )
        if data:
            return {
                "core": {
                    "remaining": data["resources"]["core"]["remaining"],
                    "reset": data["resources"]["core"]["reset"],
                },
                "search": {
                    "remaining": data["resources"]["search"]["remaining"],
                    "reset": data["resources"]["search"]["reset"],
                },
            }
    except GitHubAPIError as e:
        console.print(f"[red]Error getting rate limit status: {e}[/red]")
        logging.error(f"Error getting rate limit status: {e}")
        return None
    except Exception as e:
        console.print(
            f"[red]Unexpected error getting rate limit status: {e}[/red]"
        )
        logging.exception(f"Unexpected error getting rate limit status: {e}")
        return None


def search_repositories(token, max_repos, start_date, end_date):
    """Search for *public* Python repositories, respecting rate limits."""
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": "GitHub-Repo-Scraper/1.0",
    }
    query = f"language:python pushed:{start_date}..{end_date} is:public"

    repos = []
    page = 1
    per_page = 100

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "[cyan]Searching repositories...", total=max_repos
        )
        while len(repos) < max_repos:
            rate_limit = get_rate_limit_status(token)
            if rate_limit and rate_limit["search"]["remaining"] <= 1:
                reset_time = datetime.fromtimestamp(
                    rate_limit["search"]["reset"]
                )
                wait_time = (reset_time - datetime.now()).total_seconds() + 1
                wait_time = max(0, wait_time)
                console.print(
                    f"[yellow]Search rate limit low. Waiting until {reset_time.strftime('%Y-%m-%d %H:%M:%S')} ({wait_time:.0f} seconds)[/yellow]"
                )
                logging.warning(
                    f"Search rate limit low. Waiting until {reset_time.strftime('%Y-%m-%d %H:%M:%S')} ({wait_time:.0f} seconds)"
                )
                progress.stop()
                time.sleep(wait_time)
                progress.start()

            try:
                data = make_github_request(
                    f"{GITHUB_API_URL}/search/repositories",
                    headers=headers,
                    params={
                        "q": query,
                        "sort": "updated",
                        "order": "desc",
                        "per_page": per_page,
                        "page": page,
                    },
                )

                if "items" not in data or not data["items"]:
                    break

                for item in data["items"]:
                    repos.append(item)
                    progress.update(task, advance=1)
                    if len(repos) >= max_repos:
                        break

                page += 1
                if len(data["items"]) < per_page:
                    break

            except GitHubAPIError as e:
                console.print(
                    f"[red]Error during repository search: {e}[/red]"
                )
                logging.error(f"Error during repository search: {e}")
                break

        progress.update(task, completed=min(len(repos), max_repos))

    return repos[:max_repos]


def get_file_content_and_stats(token, repo_full_name, file_path):
    """Gets file content, line count, and comment ratio."""
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": "GitHub-Repo-Scraper/1.0",
    }
    try:
        file_data = make_github_request(
            f"{GITHUB_API_URL}/repos/{repo_full_name}/contents/{file_path}",
            headers=headers,
        )

        if "content" in file_data:
            decoded_content = base64.b64decode(file_data["content"]).decode(
                "utf-8", errors="replace"
            )
            lines = decoded_content.splitlines()
            num_lines = len(lines)

            if num_lines == 0:
                return "", 0, 0.0

            comment_lines = 0
            for line in lines:
                if re.match(r"^\s*#", line):
                    comment_lines += 1

            code_lines = num_lines - comment_lines
            comment_ratio = (
                (comment_lines / code_lines) * 100 if code_lines > 0 else 0.0
            )
            return decoded_content, num_lines, comment_ratio

    except GitHubAPIError as e:
        logging.error(
            f"Error getting file stats for {repo_full_name}/{file_path}: {e}"
        )
        return "", 0, 0.0


def find_python_files(
    token, repo_full_name, min_lines, max_lines, quality_threshold
):
    """Finds Python files, excluding dependency directories."""
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": "GitHub-Repo-Scraper/1.0",
    }
    results = []

    exclude_patterns = [
        r"^env/",
        r"^venv/",
        r".*/site-packages/",
        r".*/dist-packages/",
        r"^tests/",
        r"^docs/",
        r"^\.",  # Hidden directories
    ]

    try:
        # --- ADDED DEBUGGING HERE ---
        repo_info = None  # Initialize to None
        try:
            repo_info = make_github_request(
                f"{GITHUB_API_URL}/repos/{repo_full_name}", headers=headers
            )
        except GitHubAPIError as e:
            logging.error(
                f"Failed to fetch repository info for {repo_full_name}: {e}"
            )
            return []  # Exit early if we can't even get repo info

        if (
            not isinstance(repo_info, dict)
            or "default_branch" not in repo_info
        ):
            logging.warning(
                f"Repository info for {repo_full_name} is invalid or missing 'default_branch'.  Repo info: {repo_info}"
            )
            return []  # Exit if repo_info is invalid

        default_branch = repo_info["default_branch"]
        # --- END ADDED DEBUGGING ---

        tree_data = make_github_request(
            f"{GITHUB_API_URL}/repos/{repo_full_name}/git/trees/{default_branch}?recursive=1",
            headers=headers,
        )
        if "tree" not in tree_data:
            return []

        for item in tree_data["tree"]:
            if item["type"] == "blob" and item["path"].endswith(".py"):
                if any(
                    re.search(pattern, item["path"])
                    for pattern in exclude_patterns
                ):
                    continue

                _, num_lines, comment_ratio = get_file_content_and_stats(
                    token, repo_full_name, item["path"]
                )

                if (
                    min_lines <= num_lines <= max_lines
                    and comment_ratio >= quality_threshold
                ):
                    results.append((item["path"], num_lines, comment_ratio))

    except GitHubAPIError as e:
        logging.error(f"Error during file search in {repo_full_name}: {e}")
    return results


def create_unique_filename(
    base_name,
    max_repos,
    min_lines,
    max_lines,
    quality_threshold,
    start_date,
    end_date,
    extension,
):
    """Creates a unique filename including all filter parameters."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    start_date_str = start_date.replace("-", "")
    end_date_str = end_date.replace("-", "")
    return f"{timestamp}-{max_repos}repos-Min{min_lines}-Max{max_lines}-Quality{quality_threshold}-{start_date_str}-{end_date_str}.{extension}"


def load_existing_data(directory):
    """Loads existing data from JSON files."""
    existing_data = set()
    for filename in os.listdir(directory):
        if filename.endswith(".json"):
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            if "repo_url" in item and "python_file" in item:
                                existing_data.add(
                                    (item["repo_url"], item["python_file"])
                                )
            except (json.JSONDecodeError, FileNotFoundError) as e:
                console.print(
                    f"[yellow]Warning: Could not read {filename}: {e}[/yellow]"
                )
                logging.warning(f"Could not read {filename}: {e}")
    return existing_data


def process_repository(
    token,
    repo,
    min_lines,
    max_lines,
    quality_threshold,
    initial_existing_data,
    processed_files,
    processed_files_lock,
    progress,
    repo_task,
):
    """Processes a single repository."""
    repo_name = repo["full_name"]
    repo_url = repo["html_url"]
    results = []
    skipped_count = 0

    python_files = find_python_files(
        token, repo_name, min_lines, max_lines, quality_threshold
    )

    for file_path, num_lines, comment_ratio in python_files:
        if (repo_url, file_path) not in initial_existing_data:
            with processed_files_lock:
                if (repo_url, file_path) not in processed_files:
                    results.append(
                        {
                            "repo_url": repo_url,
                            "python_file": file_path,
                            "num_lines": num_lines,
                            "comment_ratio": comment_ratio,
                        }
                    )
                    processed_files.add((repo_url, file_path))
        else:
            skipped_count += 1

    progress.update(repo_task, advance=1)
    return results, skipped_count


def main():
    parser = argparse.ArgumentParser(description="GitHub Python Repo Scraper")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        console.print(
            "[red]Error: GitHub token not found.  Set the GITHUB_TOKEN environment variable.[/red]"
        )
        return

    parser.add_argument(
        "--max-repos", type=int, default=10, help="Number of repositories"
    )
    parser.add_argument(
        "--output", default="output", help="Base name for output"
    )
    parser.add_argument(
        "--min-lines", type=int, default=1, help="Minimum number of lines"
    )
    parser.add_argument(
        "--max-lines", type=int, default=100, help="Maximum number of lines"
    )
    parser.add_argument(
        "--quality-threshold",
        type=float,
        default=0.0,
        help="Minimum comment-to-code ratio (percentage)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="Maximum number of concurrent workers (threads)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
        help="Start date for repository search (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="End date for repository search (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    max_repos = args.max_repos
    output_base_name = args.output
    min_lines = args.min_lines
    max_lines = args.max_lines
    quality_threshold = args.quality_threshold
    max_workers = args.max_workers
    start_date = args.start_date
    end_date = args.end_date

    try:
        start_date_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_date_dt = datetime.strptime(end_date, "%Y-%m-%d")
        if start_date_dt > end_date_dt:
            console.print(
                "[red]Error: --start-date cannot be after --end-date[/red]"
            )
            return
    except ValueError:
        console.print(
            "[red]Error: Invalid date format. Please use YYYY-MM-DD.[/red]"
        )
        return

    if min_lines > max_lines:
        console.print(
            "[red]Error: --min-lines cannot be greater than --max-lines[/red]"
        )
        return

    output_file = create_unique_filename(
        output_base_name,
        max_repos,
        min_lines,
        max_lines,
        quality_threshold,
        start_date,
        end_date,
        "json",
    )
    script_directory = os.path.dirname(os.path.abspath(__file__))

    processed_files_lock = threading.Lock()
    processed_files = set()
    existing_data = load_existing_data(script_directory)

    # --- ADDED MORE ROBUST RATE LIMIT CHECK AT START ---
    try:
        rate_limit_status = get_rate_limit_status(token)
        if rate_limit_status is None:
            console.print(
                "[red]Failed to retrieve initial rate limit status. Exiting.[/red]"
            )
            return

        console.print(
            f"[green]Initial Rate Limit Status: Core Remaining: {rate_limit_status['core']['remaining']}, Search Remaining: {rate_limit_status['search']['remaining']}[/green]"
        )

        if rate_limit_status["core"]["remaining"] == 0:
            reset_time = datetime.fromtimestamp(
                rate_limit_status["core"]["reset"]
            )
            wait_time = (reset_time - datetime.now()).total_seconds() + 1
            wait_time = max(0, wait_time)
            console.print(
                f"[yellow]Core rate limit is 0. Waiting until {reset_time.strftime('%Y-%m-%d %H:%M:%S')} ({wait_time:.0f} seconds)[/yellow]"
            )
            time.sleep(wait_time)
            # Get the rate limit status again after waiting
            rate_limit_status = get_rate_limit_status(token)
            console.print(
                f"[green]Rate Limit Status After Waiting: Core Remaining: {rate_limit_status['core']['remaining']}, Search Remaining: {rate_limit_status['search']['remaining']}[/green]"
            )

    except Exception as e:
        print(f"Error during initial rate limit check: {e}")
        return
    # --- END ADDED RATE LIMIT CHECK ---

    repos = search_repositories(token, max_repos, start_date, end_date)
    if not repos:
        console.print(
            "[yellow]No public repositories found matching the criteria.[/yellow]"
        )
        return

    all_results = []
    total_skipped = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:

        repo_task = progress.add_task(
            "[green]Processing repositories...", total=len(repos)
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:
            futures = [
                executor.submit(
                    process_repository,
                    token,
                    repo,
                    min_lines,
                    max_lines,
                    quality_threshold,
                    existing_data.copy(),
                    processed_files,
                    processed_files_lock,
                    progress,
                    repo_task,
                )
                for repo in repos
            ]

            for future in concurrent.futures.as_completed(futures):
                try:
                    results, skipped_count = future.result()
                    all_results.extend(results)
                    total_skipped += skipped_count
                except Exception as e:
                    console.print(
                        f"[red]Error processing a repository: {e}[/red]"
                    )
                    logging.exception(f"Error processing a repository: {e}")

    console.print(f"[cyan]{len(all_results)} Items added to JSON[/cyan]")
    console.print(
        f"[yellow]{total_skipped} Items skipped (already present)[/yellow]"
    )

    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=4)

    console.print(f"[bold green]Results saved to {output_file}[/bold green]")


if __name__ == "__main__":
    main()
