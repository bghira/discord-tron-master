from .base import db


class ZorkCampaign(db.Model):
    __tablename__ = "zork_campaigns"
    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.BigInteger(), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    created_by = db.Column(db.BigInteger(), nullable=False)
    summary = db.Column(db.Text(), nullable=False, default="")
    state_json = db.Column(db.Text(), nullable=False, default="{}")
    characters_json = db.Column(db.Text(), nullable=False, default="{}")
    last_narration = db.Column(db.Text(), nullable=True)
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated = db.Column(db.DateTime, nullable=False, default=db.func.now())

    __table_args__ = (
        db.UniqueConstraint("guild_id", "name", name="uq_zork_campaign_guild_name"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "name": self.name,
            "created_by": self.created_by,
            "summary": self.summary,
            "state_json": self.state_json,
            "characters_json": self.characters_json,
            "last_narration": self.last_narration,
            "created": self.created,
            "updated": self.updated,
        }


class ZorkChannel(db.Model):
    __tablename__ = "zork_channels"
    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.BigInteger(), nullable=False)
    channel_id = db.Column(db.BigInteger(), nullable=False)
    enabled = db.Column(db.Boolean(), nullable=False, default=False)
    active_campaign_id = db.Column(
        db.Integer, db.ForeignKey("zork_campaigns.id"), nullable=True
    )
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated = db.Column(db.DateTime, nullable=False, default=db.func.now())

    __table_args__ = (
        db.UniqueConstraint(
            "guild_id", "channel_id", name="uq_zork_channel_guild_channel"
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "enabled": self.enabled,
            "active_campaign_id": self.active_campaign_id,
            "created": self.created,
            "updated": self.updated,
        }


class ZorkPlayer(db.Model):
    __tablename__ = "zork_players"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer, db.ForeignKey("zork_campaigns.id"), nullable=False
    )
    user_id = db.Column(db.BigInteger(), nullable=False)
    level = db.Column(db.Integer, nullable=False, default=1)
    xp = db.Column(db.Integer, nullable=False, default=0)
    attributes_json = db.Column(db.Text(), nullable=False, default="{}")
    state_json = db.Column(db.Text(), nullable=False, default="{}")
    last_active = db.Column(db.DateTime, nullable=False, default=db.func.now())
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated = db.Column(db.DateTime, nullable=False, default=db.func.now())

    __table_args__ = (
        db.UniqueConstraint(
            "campaign_id", "user_id", name="uq_zork_player_campaign_user"
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "user_id": self.user_id,
            "level": self.level,
            "xp": self.xp,
            "attributes_json": self.attributes_json,
            "state_json": self.state_json,
            "last_active": self.last_active,
            "created": self.created,
            "updated": self.updated,
        }


class ZorkTurn(db.Model):
    __tablename__ = "zork_turns"
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer, db.ForeignKey("zork_campaigns.id"), nullable=False
    )
    user_id = db.Column(db.BigInteger(), nullable=True)
    kind = db.Column(db.String(32), nullable=False)  # player, narrator, system
    content = db.Column(db.Text(), nullable=False)
    discord_message_id = db.Column(db.BigInteger(), nullable=True)
    user_message_id = db.Column(db.BigInteger(), nullable=True)
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "user_id": self.user_id,
            "kind": self.kind,
            "content": self.content,
            "discord_message_id": self.discord_message_id,
            "user_message_id": self.user_message_id,
            "created": self.created,
        }


class ZorkSnapshot(db.Model):
    __tablename__ = "zork_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    turn_id = db.Column(
        db.Integer, db.ForeignKey("zork_turns.id"), nullable=False, unique=True
    )
    campaign_id = db.Column(
        db.Integer, db.ForeignKey("zork_campaigns.id"), nullable=False
    )
    campaign_state_json = db.Column(db.Text(), nullable=False)
    campaign_characters_json = db.Column(db.Text(), nullable=False)
    campaign_summary = db.Column(db.Text(), nullable=False, default="")
    campaign_last_narration = db.Column(db.Text(), nullable=True)
    players_json = db.Column(db.Text(), nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=db.func.now())
