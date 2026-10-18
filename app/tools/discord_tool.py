import logging
import httpx
from app.config import DISCORD_BOT_TOKEN

logger = logging.getLogger(__name__)
DISCORD_API_BASE = "https://discord.com/api/v10"


def _get_headers() -> dict:
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN is not configured in .env")
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def get_bot_guilds() -> list[dict]:
    """Fetch all Discord servers (guilds) where the bot is currently a member."""
    headers = _get_headers()
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{DISCORD_API_BASE}/users/@me/guilds", headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch guilds: %s %s", resp.status_code, resp.text)
            return []
        return resp.json()


def get_guild_details(guild_id: str) -> dict:
    """Fetch complete guild information including channels and roles."""
    headers = _get_headers()
    with httpx.Client(timeout=15.0) as client:
        # 1. Guild info
        g_resp = client.get(f"{DISCORD_API_BASE}/guilds/{guild_id}", headers=headers)
        if g_resp.status_code != 200:
            return {"ok": False, "error": f"Failed to get guild: {g_resp.status_code}"}
        guild_info = g_resp.json()

        # 2. Channels
        ch_resp = client.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=headers)
        channels = ch_resp.json() if ch_resp.status_code == 200 else []

        # 3. Roles
        roles_resp = client.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles", headers=headers)
        roles = roles_resp.json() if roles_resp.status_code == 200 else []

        return {
            "ok": True,
            "guild": guild_info,
            "channels": channels,
            "roles": roles,
        }


def create_role(guild_id: str, name: str, color: int = 0, hoist: bool = True, mentionable: bool = False) -> dict:
    """Create a new role in the guild."""
    headers = _get_headers()
    payload = {
        "name": name,
        "color": color,
        "hoist": hoist,
        "mentionable": mentionable,
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            return {"ok": True, "role": resp.json()}
        return {"ok": False, "error": resp.text, "status": resp.status_code}


def create_channel(guild_id: str, name: str, channel_type: int = 0, parent_id: str | None = None, topic: str = "") -> dict:
    """
    Create a channel in the guild.
    channel_type:
      0 = GUILD_TEXT (text channel)
      2 = GUILD_VOICE (voice channel)
      4 = GUILD_CATEGORY (category)
      5 = GUILD_ANNOUNCEMENT
    """
    headers = _get_headers()
    payload = {
        "name": name,
        "type": channel_type,
    }
    if parent_id:
        payload["parent_id"] = parent_id
    if topic and channel_type == 0:
        payload["topic"] = topic

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            return {"ok": True, "channel": resp.json()}
        return {"ok": False, "error": resp.text, "status": resp.status_code}


def send_channel_message(channel_id: str, content: str = "", embeds: list[dict] | None = None) -> dict:
    """Send a message or embed to a specific channel."""
    headers = _get_headers()
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages", headers=headers, json=payload)
        if resp.status_code in (200, 201):
            return {"ok": True, "message": resp.json()}
        return {"ok": False, "error": resp.text, "status": resp.status_code}


def execute_server_blueprint(guild_id: str, blueprint: dict) -> dict:
    """
    Apply an entire approved server architecture blueprint:
    - Creates roles
    - Creates categories and nested channels
    - Posts welcome/rules embeds
    """
    results = {"roles_created": [], "categories_created": [], "channels_created": [], "errors": []}

    # 1. Create Roles
    for role_spec in blueprint.get("roles", []):
        res = create_role(
            guild_id=guild_id,
            name=role_spec["name"],
            color=role_spec.get("color", 0),
            hoist=role_spec.get("hoist", True),
        )
        if res.get("ok"):
            results["roles_created"].append(role_spec["name"])
        else:
            results["errors"].append(f"Role {role_spec['name']}: {res.get('error')}")

    # 2. Create Categories and Channels
    welcome_channel_id = None
    for cat_spec in blueprint.get("categories", []):
        cat_name = cat_spec["name"]
        cat_res = create_channel(guild_id=guild_id, name=cat_name, channel_type=4)
        cat_id = None
        if cat_res.get("ok"):
            cat_id = cat_res["channel"]["id"]
            results["categories_created"].append(cat_name)
        else:
            results["errors"].append(f"Category {cat_name}: {cat_res.get('error')}")

        for ch_spec in cat_spec.get("channels", []):
            ch_type = 2 if ch_spec.get("type") == "voice" else 0
            ch_res = create_channel(
                guild_id=guild_id,
                name=ch_spec["name"],
                channel_type=ch_type,
                parent_id=cat_id,
                topic=ch_spec.get("topic", ""),
            )
            if ch_res.get("ok"):
                results["channels_created"].append(ch_spec["name"])
                if any(k in ch_spec["name"] for k in ("rules", "قوانين", "welcome", "ترحيب")):
                    welcome_channel_id = ch_res["channel"]["id"]
            else:
                results["errors"].append(f"Channel {ch_spec['name']}: {ch_res.get('error')}")

    # 3. Post Rules / Welcome Embed if defined
    if welcome_channel_id and blueprint.get("rules_embed"):
        send_channel_message(welcome_channel_id, embeds=[blueprint["rules_embed"]])

    return results
