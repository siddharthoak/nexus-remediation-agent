"""
SCR-02: Update script — build, push, and deploy/update the Fixer and Watcher agents.

Run every time code changes are shipped. Idempotent: creates agents if they don't exist,
updates them if they do.

Prerequisites:
  1. cp config.yaml.example config.yaml  (first time only)
  2. Fill in config.yaml with your values.
  3. Run scripts/bootstrap_foundry_project.sh  (fills in the infra section).
  4. az acr login --name <acr_name>  (or Docker login to your ACR).
  5. python3 scripts/update_agent.py

All configuration is read from config.yaml. No environment variables are required,
but env vars override config.yaml values if both are set (useful for CI pipelines).

NOTE: AIProjectClient method signatures (create_agent, update_agent, list_agents) must
be validated against current azure-ai-projects SDK docs:
https://learn.microsoft.com/en-us/python/api/azure-ai-projects
"""

import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Run: pip install PyYAML")
    sys.exit(1)

try:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential
except ImportError:
    print("ERROR: Required packages missing. Run: pip install azure-ai-projects azure-identity")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
CONFIG_FILE = REPO_ROOT / "config.yaml"

AGENTS = [
    {
        "name": "nexus-fixer-agent",
        "image_name": "fixer-agent",
        "dockerfile_context": str(REPO_ROOT),
        "dockerfile": str(REPO_ROOT / "agents" / "fixer" / "Dockerfile"),
        "instructions_file": str(REPO_ROOT / "agents" / "fixer" / "instructions.md"),
        "agent_yaml": str(REPO_ROOT / "infra" / "agents" / "fixer.agent.yaml"),
    },
    {
        "name": "nexus-watcher-agent",
        "image_name": "watcher-agent",
        "dockerfile_context": str(REPO_ROOT),
        "dockerfile": str(REPO_ROOT / "agents" / "watcher" / "Dockerfile"),
        "instructions_file": str(REPO_ROOT / "agents" / "watcher" / "instructions.md"),
        "agent_yaml": str(REPO_ROOT / "infra" / "agents" / "watcher.agent.yaml"),
    },
]


# ── Config loading ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"ERROR: {CONFIG_FILE} not found.")
        print("  Run: cp config.yaml.example config.yaml")
        print("  Then fill in your values and run: bash scripts/bootstrap_foundry_project.sh")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    return cfg


def build_substitutions(cfg: dict, image_tag: str) -> dict:
    """
    Flatten config.yaml into a dict of PLACEHOLDER → value used to resolve
    all {{PLACEHOLDER}} tokens in the agent YAML files.

    Environment variables take precedence over config.yaml values, allowing
    CI pipelines to override specific values without editing the file.
    """
    def env_or(env_name: str, config_value) -> str:
        return os.environ.get(env_name) or str(config_value or "")

    return {
        # Infrastructure (from bootstrap)
        "ACR_LOGIN_SERVER":          env_or("ACR_LOGIN_SERVER",        cfg["infra"]["acr_login_server"]),
        "KEY_VAULT_URI":             env_or("KEY_VAULT_URI",           cfg["infra"]["key_vault_uri"]),
        "COSMOS_ENDPOINT":           env_or("COSMOS_ENDPOINT",         cfg["infra"]["cosmos_endpoint"]),
        # .get() with a default here (unlike the other infra.* keys above): a
        # config.yaml written before OBS-02 was added won't have this key yet,
        # and telemetry.py already treats a blank value as "disabled" safely.
        "APPLICATIONINSIGHTS_CONNECTION_STRING": env_or(
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            cfg["infra"].get("app_insights_connection_string", ""),
        ),

        # Azure AI Foundry
        "PROJECT_ENDPOINT":          env_or("PROJECT_ENDPOINT",        cfg["foundry"]["project_endpoint"]),
        "MODEL_DEPLOYMENT_NAME":     env_or("MODEL_DEPLOYMENT_NAME",   cfg["foundry"]["model_deployment_name"]),
        "FIXER_AGENT_ID":            env_or("FIXER_AGENT_ID",          cfg["foundry"]["fixer_agent_id"]),

        # GitHub
        "GITHUB_REPO_TARGET":        env_or("GITHUB_REPO_TARGET",      cfg["github"]["repo_target"]),

        # Nexus IQ
        "NEXUS_IQ_ENDPOINT":         env_or("NEXUS_IQ_ENDPOINT",       cfg["nexus"]["endpoint"]),
        "NEXUS_IQ_APP_PUBLIC_ID":    env_or("NEXUS_IQ_APP_PUBLIC_ID",  cfg["nexus"]["app_public_id"]),

        # Schedules
        "FIXER_SCHEDULE":            env_or("FIXER_SCHEDULE",          cfg["schedules"]["fixer"]),
        "WATCHER_SCHEDULE":          env_or("WATCHER_SCHEDULE",        cfg["schedules"]["watcher"]),

        # Runtime
        "MAX_RETRY_ATTEMPTS":        env_or("MAX_RETRY_ATTEMPTS",      cfg["runtime"]["max_retry_attempts"]),
        "CI_POLL_INTERVAL":          env_or("CI_POLL_INTERVAL",        cfg["runtime"]["ci_poll_interval"]),
        "CI_TIMEOUT_SECONDS":        env_or("CI_TIMEOUT_SECONDS",      cfg["runtime"]["ci_timeout_seconds"]),
        "COSMOS_DATABASE":           env_or("COSMOS_DATABASE",         cfg["runtime"]["cosmos_database"]),
        "COSMOS_CONTAINER":          env_or("COSMOS_CONTAINER",        cfg["runtime"]["cosmos_container"]),

        # Key Vault secret names
        "NEXUS_IQ_API_KEY_SECRET_NAME": env_or("NEXUS_IQ_API_KEY_SECRET_NAME", cfg["secrets"]["nexus_iq_api_key"]),
        "GITHUB_PAT_SECRET_NAME":       env_or("GITHUB_PAT_SECRET_NAME",       cfg["secrets"]["github_pat"]),
        "ANTHROPIC_API_KEY_SECRET_NAME": env_or("ANTHROPIC_API_KEY_SECRET_NAME", cfg["secrets"]["anthropic_api_key"]),

        # Computed at deploy time
        "IMAGE_TAG": image_tag,
    }


def substitute(text: str, subs: dict) -> str:
    """Replace every {{PLACEHOLDER}} in text with the corresponding value."""
    for key, value in subs.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def check_unresolved(rendered: str, source_path: str) -> None:
    """Warn if any {{PLACEHOLDER}} tokens remain after substitution."""
    remaining = re.findall(r"\{\{[A-Z_]+\}\}", rendered)
    if remaining:
        unique = sorted(set(remaining))
        print(f"  WARNING: {source_path} has unresolved placeholders: {', '.join(unique)}")
        print("  Check config.yaml — these values may not be set yet.")


def render_agent_yaml(agent: dict, subs: dict) -> str:
    """Read an agent YAML template and return the fully substituted text."""
    with open(agent["agent_yaml"]) as f:
        raw = f.read()
    rendered = substitute(raw, subs)
    check_unresolved(rendered, agent["agent_yaml"])
    return rendered


# ── Git / Docker ──────────────────────────────────────────────────────────────

def get_git_short_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print("WARNING: Could not get git SHA. Using 'latest' as image tag.")
        return "latest"
    return result.stdout.strip()


def build_image(agent: dict, acr_login_server: str, image_tag: str) -> str:
    full_image = f"{acr_login_server}/{agent['image_name']}:{image_tag}"
    print(f"\n>>> Building image: {full_image}")
    result = subprocess.run(
        ["docker", "build", "-t", full_image, "-f", agent["dockerfile"], agent["dockerfile_context"]],
        check=False,
    )
    if result.returncode != 0:
        print(f"ERROR: Docker build failed for {agent['name']}.")
        sys.exit(1)
    return full_image


def push_image(full_image: str) -> None:
    print(f">>> Pushing image: {full_image}")
    result = subprocess.run(["docker", "push", full_image], check=False)
    if result.returncode != 0:
        print(f"ERROR: Docker push failed for {full_image}.")
        print("  Ensure you have run: az acr login --name <acr_name>")
        sys.exit(1)


# ── AAF agent registration ────────────────────────────────────────────────────

def load_instructions(agent: dict) -> str:
    with open(agent["instructions_file"]) as f:
        return f.read()


def find_agent_by_name(client, agent_name: str):
    """
    Return the existing agent object if found, None otherwise.
    NOTE: Confirm method name against current azure-ai-projects SDK docs —
    may be client.agents.list() or client.get_agents().
    """
    try:
        for agent in client.agents.list_agents():
            if agent.name == agent_name:
                return agent
    except Exception as exc:
        print(f"  WARNING: Could not list existing agents: {exc}")
        print("  Assuming agent does not exist — will attempt create.")
    return None


def create_or_update_agent(client, agent_config: dict, subs: dict) -> None:
    """
    Create or update a Hosted Agent definition in Azure AI Foundry.
    NOTE: AIProjectClient.agents.create_agent / update_agent parameter names must be
    validated against current SDK docs before relying on them.
    """
    agent_name = agent_config["name"]
    instructions = load_instructions(agent_config)

    print(f">>> Checking if agent '{agent_name}' exists...")
    existing = find_agent_by_name(client, agent_name)

    agent_kwargs = {
        "name": agent_name,
        "instructions": instructions,
        "model": subs["MODEL_DEPLOYMENT_NAME"],
        # NOTE: Confirm whether container image is passed here or in a separate resource spec.
        # "container_image": full_image,
    }

    if existing:
        print(f"  Agent '{agent_name}' exists — updating.")
        client.agents.update_agent(existing.id, **agent_kwargs)
        print(f"  Updated agent '{agent_name}'.")
    else:
        print(f"  Agent '{agent_name}' not found — creating.")
        client.agents.create_agent(**agent_kwargs)
        print(f"  Created agent '{agent_name}'.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("  OSS Remediation Agent — Update Script")
    print("=" * 64)
    print()

    cfg = load_config()
    image_tag = get_git_short_sha()
    subs = build_substitutions(cfg, image_tag)

    acr_login_server = subs["ACR_LOGIN_SERVER"]
    project_endpoint = subs["PROJECT_ENDPOINT"]

    if not acr_login_server:
        print("ERROR: infra.acr_login_server is not set in config.yaml.")
        print("  Run: bash scripts/bootstrap_foundry_project.sh first.")
        sys.exit(1)
    if not project_endpoint:
        print("ERROR: foundry.project_endpoint is not set in config.yaml.")
        print("  Set it after creating the Azure AI Foundry project.")
        sys.exit(1)

    print(f"Image tag (git SHA): {image_tag}")
    print(f"ACR:                 {acr_login_server}")
    print(f"Foundry endpoint:    {project_endpoint}")
    print()

    # Render and preview substituted YAMLs (useful for debugging)
    print(">>> Rendering agent YAML templates...")
    for agent in AGENTS:
        rendered = render_agent_yaml(agent, subs)
        rendered_path = Path(agent["agent_yaml"]).with_suffix(".rendered.yaml")
        rendered_path.write_text(rendered)
        print(f"  Written: {rendered_path.name}  (preview only — not deployed directly)")
    print()

    # Build and push images
    built_images = {}
    for agent in AGENTS:
        full_image = build_image(agent, acr_login_server, image_tag)
        push_image(full_image)
        built_images[agent["name"]] = full_image

    # Register with Azure AI Foundry
    print(f"\n>>> Connecting to Azure AI Foundry: {project_endpoint}")
    client = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())
    print("  Connected.\n")

    for agent in AGENTS:
        create_or_update_agent(client=client, agent_config=agent, subs=subs)

    print()
    print("=" * 64)
    print("  All agents deployed successfully.")
    print(f"  Image tag: {image_tag}")
    print("=" * 64)
    print()
    print("NOTE: If this was the first Fixer deploy, retrieve its agent ID from the")
    print("AAF portal and set foundry.fixer_agent_id in config.yaml, then re-run")
    print("this script so the Watcher gets FIXER_AGENT_ID in its environment.")


if __name__ == "__main__":
    main()
