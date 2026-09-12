"""The shared public NAT column contract; all other fields stay in the backend."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

DISPLAY_COLUMNS=(('private_ip','Private IP'),('private_port','Private Port'),
    ('public_ip','Public IP'),('public_port','Public Port'),('destination_ip','Dest IP'),
    ('destination_port','Dest Port'),('protocol','Protocol/App'),
    ('timestamp','Session Start Time'),('subscriber_id','Subscriber/User ID'))
DISPLAY_FIELDS=tuple(key for key,_ in DISPLAY_COLUMNS)
DISPLAY_TIMEZONE='Asia/Karachi'


def export_value(key,value):
    if value is None:return ''
    if key=='timestamp':
        if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')
    return value
