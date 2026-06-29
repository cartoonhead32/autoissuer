import sys
import os
import subprocess
import json
import time
import re
from pathlib import Path

def run_git_command(args, cwd):
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def run_gh_command(args, cwd):
    result = subprocess.run(["gh"] + args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def get_repo_info(workspace_dir):
    try:
        output = run_gh_command(["repo", "view", "--json", "name,owner"], workspace_dir)
        data = json.loads(output)
        return data.get("name"), data.get("owner", {}).get("login")
    except Exception as e:
        print(f"Warning: Could not auto-detect repository info ({e})")
        return None, None

def get_github_project(workspace_dir):
    # Allow override via environment variable
    env_project = os.environ.get("GH_PROJECT")
    if env_project:
        return env_project

    repo_name, repo_owner = get_repo_info(workspace_dir)

    try:
        args = ["project", "list", "--format", "json"]
        if repo_owner:
            args += ["--owner", repo_owner]

        output = run_gh_command(args, workspace_dir)
        data = json.loads(output)

        # Phase 1: Try to find an open project that contains the repo name in its title (case-insensitive)
        if repo_name:
            for p in data.get("projects", []):
                if not p.get("closed") and repo_name.lower() in p.get("title", "").lower():
                    return p.get("title")

        # Phase 2: Fall back to the first open project in the list if no match is found
        for p in data.get("projects", []):
            if not p.get("closed"):
                return p.get("title")
    except Exception as e:
        print(f"Warning: Could not auto-detect GitHub project ({e})")
    return None

def get_existing_labels(workspace_dir):
    try:
        output = run_gh_command(["label", "list", "--json", "name"], workspace_dir)
        labels_data = json.loads(output)
        return {l["name"].lower() for l in labels_data}
    except Exception as e:
        print(f"Warning: Could not fetch repository labels ({e})")
        return set()

def ensure_label_exists(label, existing_labels, workspace_dir):
    if label.lower() in existing_labels:
        return True
    try:
        print(f"Label '{label}' not found. Creating it...")
        run_gh_command(["label", "create", label, "--color", "5319e7", "--description", f"Issues related to {label}"], workspace_dir)
        existing_labels.add(label.lower())
        return True
    except Exception as e:
        print(f"Warning: Could not create label '{label}' ({e})")
        return False

def get_commits_context(shas, workspace_dir):
    resolved_commits = []
    diff_content = []

    for sha in shas:
        full_sha = run_git_command(["rev-parse", sha], workspace_dir)
        msg = run_git_command(["log", "-1", "--format=%s", sha], workspace_dir)
        resolved_commits.append((full_sha, msg))

        # Get list of changed files for complete context
        file_status = run_git_command(["show", "--name-status", "--oneline", sha], workspace_dir)

        # Get git show diff excluding assets, lock files, and binaries
        diff = run_git_command([
            "show", sha, "--", ".",
            ":(exclude)paintings/*", ":(exclude)img/*", ":(exclude)vendor/*",
            ":(exclude)node_modules/*", ":(exclude)*lock*", ":(exclude)*.phar",
            ":(exclude)*.png", ":(exclude)*.jpg", ":(exclude)*.jpeg", ":(exclude)*.gif", ":(exclude)*.webp"
        ], workspace_dir)

        # Truncate diff if it is too large to save token quota
        max_diff_len = 25000
        if len(diff) > max_diff_len:
            diff = diff[:max_diff_len] + f"\n\n... [Diff truncated to save API token quota (original size: {len(diff)} chars)] ..."

        full_context = f"Changed Files:\n{file_status}\n\nDiff Content:\n{diff}"
        diff_content.append(full_context)

    return resolved_commits, "\n\n".join(diff_content)

def run_hybrid_mode(shas, workspace_dir):
    # Fetch git context
    resolved_commits, git_diff = get_commits_context(shas, workspace_dir)

    commits_str = "\n".join(f"- {sha}: {msg}" for sha, msg in resolved_commits)

    # Prompt for the AI to summarize and label (flat list requirement enforced)
    prompt = f"""
You are an expert developer. Analyze the following Git commits, file lists, and diffs.
Generate:
1. A professional, concise summary of what the changes do in the present tense in list form (Markdown format) to be used as an issue description. Limit the summary to at most 3-5 high-level bullet points summarizing the core functional changes in the present tense, keeping it brief and to the point.
   CRITICAL: The description must consist ONLY of a list of bullet points (e.g. - detail1\\n- detail2) with NO headers, markdown titles, or introductory/wrapping text. Do not include commit hashes in the description text itself.
2. Based on the files changed, classify the changes into "Frontend" (if modifying UI, views/, public/, CSS/JS, HTML templates), "Backend" (if modifying server.js, backend APIs, DB files, docker compose), or both.
3. A concise and descriptive issue title.

Commits:
{commits_str}

Context & Diff:
{git_diff}

You must respond with a JSON object in this format:
{{
  "title": "A short descriptive title",
  "markdown": "- Concise detail 1\\n- Concise detail 2",
  "labels": ["Backend"]
}}
"""

    print("Requesting IBM Bob Shell to summarize commits and determine categories...")

    response = None
    last_error = None
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # Call Bob Shell CLI with API key authentication
            result = subprocess.run(
                ["bob", "--auth-method", "api-key", "-p", prompt],
                capture_output=True,
                text=True,
                check=True,
                timeout=120
            )
            response = result.stdout.strip()
            break
        except subprocess.TimeoutExpired:
            last_error = Exception("Bob Shell request timed out")
            if attempt < max_retries - 1:
                print(f"Request timed out (attempt {attempt + 1}/{max_retries}). Retrying...")
                time.sleep(2.0)
            else:
                print(f"Error: All retry attempts failed due to timeout.")
                raise last_error
        except subprocess.CalledProcessError as e:
            last_error = e
            err_msg = e.stderr if e.stderr else str(e)
            is_rate_limit = "rate_limit" in err_msg.lower() or "429" in err_msg

            if is_rate_limit and attempt < max_retries - 1:
                sleep_time = 5.0 * (attempt + 1)
                print(f"Rate limit hit (attempt {attempt + 1}/{max_retries}). Sleeping for {sleep_time:.2f}s before retrying...")
                time.sleep(sleep_time)
            elif attempt < max_retries - 1:
                print(f"Warning: Request failed ({err_msg}). Retrying...")
                time.sleep(2.0)
            else:
                print(f"Error: All retry attempts failed.")
                raise e

    if response is None:
        print("Error: Failed to get response from IBM Bob Shell.")
        if last_error:
            raise last_error
        sys.exit(1)

    # Extract JSON from response (may be wrapped in markdown code blocks or other text)
    response_text = response.strip()
    
    # Try to find JSON in the response
    json_match = re.search(r'\{[^{}]*"title"[^{}]*"markdown"[^{}]*"labels"[^{}]*\}', response_text, re.DOTALL)
    if json_match:
        response_text = json_match.group(0)
    elif "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()
    
    result_data = json.loads(response_text)
    title = result_data.get("title", f"Updates for commits: {', '.join(s[:7] for s, _ in resolved_commits)}")
    markdown_text = result_data.get("markdown", "")
    labels = result_data.get("labels", ["Backend"])

    # Ensure description is written to the scripts directory
    script_dir = Path(__file__).resolve().parent
    desc_path = script_dir / "issue-description.md"
    try:
        with open(desc_path, "w") as f:
            f.write(markdown_text.strip() + "\n")

        print(f"Written description to {desc_path}")
        print(f"Issue Title: '{title}'")
        print(f"Labels: {labels}")

        # Resolve labels and ensure they exist
        resolved_labels = []
        for l in labels:
            if l.lower() == "both":
                resolved_labels.extend(["Frontend", "Backend"])
            else:
                resolved_labels.append(l)

        existing_labels = get_existing_labels(workspace_dir)
        label_args = []
        for l in resolved_labels:
            if ensure_label_exists(l, existing_labels, workspace_dir):
                label_args += ["--label", l]

        project_name = get_github_project(workspace_dir)
        project_args = ["--project", project_name] if project_name else []

        print("Creating GitHub issue...")
        issue_url = run_gh_command(["issue", "create", "--title", title, "--body-file", str(desc_path), "--assignee", "@me"] + project_args + label_args, workspace_dir)
        print(f"Created issue: {issue_url}")

        # Post comment
        comment_body = "Fixed in commits:\n"
        for full_sha, msg in resolved_commits:
            comment_body += f"- {full_sha} ({msg})\n"

        print("Adding comment...")
        comment_url = run_gh_command(["issue", "comment", issue_url, "--body", comment_body], workspace_dir)
        print(f"Added comment: {comment_url}")

        # Close issue
        print("Closing issue...")
        run_gh_command(["issue", "close", issue_url], workspace_dir)
        print(f"Closed issue: {issue_url}")
    finally:
        if desc_path.exists():
            desc_path.unlink()
            print("Cleaned up temporary issue description file.")

def main():
    if len(sys.argv) < 2:
        print("Usage: make-issue.sh <commit_sha_1> [commit_sha_2 ...]")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print("Usage: make-issue.sh <commit_sha_1> [commit_sha_2 ...]")
        sys.exit(1)

    # Determine workspace directory (git repo root) based on current working directory
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        WORKSPACE_DIR = Path(result.stdout.strip())
    except Exception:
        # Fall back to script's parent directory if we are not inside a git repository
        SCRIPT_DIR = Path(__file__).resolve().parent
        WORKSPACE_DIR = SCRIPT_DIR.parent

    # Check for BOBSHELL_API_KEY in environment
    api_key = os.environ.get("BOBSHELL_API_KEY")

    # Load from .env if it exists
    env_path = WORKSPACE_DIR / ".env"
    if not api_key and env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOBSHELL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    os.environ["BOBSHELL_API_KEY"] = api_key
                    break

    # Prompt user if still missing
    if not api_key:
        print("A Bob Shell API key is required to use IBM Bob Shell.")
        print("You can get an API key from: https://bob.ibm.com/admin/apikeys")
        print("Set the Scope to 'Inference' when creating the key.")
        try:
            api_key = input("Please enter your Bob Shell API key: ").strip()
            if not api_key:
                print("Error: API key cannot be empty.")
                sys.exit(1)
            os.environ["BOBSHELL_API_KEY"] = api_key
            
            # Save to .env
            with open(env_path, "a") as f:
                f.write(f"\nBOBSHELL_API_KEY={api_key}\n")
            print(f"API key saved to {env_path}")
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(1)

    run_hybrid_mode(args, WORKSPACE_DIR)

if __name__ == "__main__":
    main()
