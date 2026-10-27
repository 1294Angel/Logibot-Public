from core import config, db

def get(guild_id) -> dict:
    row = db.q1("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
    if not row:
        return {
            "roles": set(config.DEFAULT_MANAGER_ROLE_IDS), "category": config.CATEGORY_NAME,
            "forum": config.FORUM_NAME, "social_forum": config.SOCIAL_FORUM_NAME,
            "area_matching": False, "log_channel": None,
            "category_id": None, "event_forum_id": None, "social_forum_id": None,
            "guide_id": None, "guide_hash": None, "action_channel": None, "public_addresses": True,
        }
    return {
        "roles": set(db.loads(row["manager_roles"])) or set(config.DEFAULT_MANAGER_ROLE_IDS),
        "category": row["category_name"] or config.CATEGORY_NAME,
        "forum": row["forum_name"] or config.FORUM_NAME,
        "social_forum": row["social_forum_name"] or config.SOCIAL_FORUM_NAME,
        "area_matching": bool(row["area_matching"]),
        "log_channel": row["log_channel_id"],
        "category_id": row["category_id"],
        "event_forum_id": row["event_forum_id"],
        "social_forum_id": row["social_forum_id"],
        "guide_id": row["log_guide_id"],
        "guide_hash": row["log_guide_hash"],
        "action_channel": row["action_log_channel_id"],
        "public_addresses": row["public_addresses"] is None or bool(row["public_addresses"]),
    }


def save(guild_id, roles=None, category=None, forum=None, social_forum=None, area_matching=None, log_channel=None,
         category_id=None, event_forum_id=None, social_forum_id=None, guide_id=None, guide_hash=None,
         action_channel=None, public_addresses=None):
    cursor = get(guild_id)
    db.run(
        "INSERT OR REPLACE INTO guild_settings "
        "(guild_id, manager_roles, category_name, forum_name, social_forum_name, area_matching, log_channel_id, "
        "category_id, event_forum_id, social_forum_id, log_guide_id, log_guide_hash, action_log_channel_id, public_addresses) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            guild_id,
            db.dumps(sorted(roles if roles is not None else cursor["roles"])),
            category or cursor["category"],
            forum or cursor["forum"],
            social_forum or cursor["social_forum"],
            int(cursor["area_matching"] if area_matching is None else area_matching),
            log_channel or cursor["log_channel"],
            category_id or cursor["category_id"],
            event_forum_id or cursor["event_forum_id"],
            social_forum_id or cursor["social_forum_id"],
            guide_id or cursor["guide_id"],
            guide_hash or cursor["guide_hash"],
            action_channel or cursor["action_channel"],
            int(cursor["public_addresses"] if public_addresses is None else public_addresses),
        ),
    )
