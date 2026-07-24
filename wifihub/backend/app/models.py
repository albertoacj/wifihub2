from pydantic import BaseModel, Field


class DeviceUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    pref_ap: str | None = None      # "" solta a preferência


class SteerRequest(BaseModel):
    mac: str
    target_ap: str


class RedirectCreate(BaseModel):
    proto: str = Field(pattern="^(tcp|udp|tcp udp)$")
    src_dport: str
    dest_ip: str
    dest_port: str
    name: str = ""


class RadioUpdate(BaseModel):
    channel: str | int | None = None
    txpower: int | None = None


class IconRename(BaseModel):
    name: str


class RuleCreate(BaseModel):
    name: str = ""
    src: str
    dest: str
    target: str = "ACCEPT"
    src_ip: str = ""
    dest_ip: str = ""
    proto: str = ""
    dest_port: str = ""


class RuleToggle(BaseModel):
    enabled: bool
