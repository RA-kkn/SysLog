
import re
import ipaddress
import uuid
from datetime import datetime, timezone

# RFC3164:  <PRI>MMM DD HH:MM:SS HOSTNAME TAG[PID]: MESSAGE
RFC3164_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>"
    r"(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<tag>[^\[:]+)(?:\[(?P<pid>\d+)\])?:\s*"
    r"(?P<message>.*)$"
)

# RFC5424:  <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG
RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<version>\d)\s+"
    r"(?P<timestamp>\S+)\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<app>\S+)\s+"
    r"(?P<procid>\S+)\s+"
    r"(?P<msgid>\S+)\s+"
    r"(?P<sd>(?:\[.*?\]|-))\s?"
    r"(?P<message>.*)$"
)

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _decode_pri(pri: int):
    facility = pri // 8
    severity = pri % 8
    return facility, severity


def _parse_rfc3164_time(ts: str, now: datetime) -> datetime:
    """RFC3164 mein year nahi hota, current year assume karte hain.
    Edge case: agar message December ka hai aur hum January mein receive
    kar rahe hain (late delivery), to previous year use karo."""
    mon_str, day_str, time_str = ts.split(None, 2)
    month = MONTHS.get(mon_str, now.month)
    day = int(day_str)
    hh, mm, ss = (int(x) for x in time_str.split(":"))

    year = now.year
    if month == 12 and now.month == 1:
        year -= 1

    try:
        return datetime(year, month, day, hh, mm, ss, tzinfo=timezone.utc)
    except ValueError:
        return now


def parse_syslog(raw: bytes, src_ip: str, src_port: int, received_at: datetime):
    """
    Returns a dict ready for ClickHouse insert, ya None agar parse na ho
    saka (aisi case mein bhi raw_message ke saath ek fallback row bana dete
    hain taake data loss na ho).
    """
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:
        text = str(raw)

    m5424 = RFC5424_RE.match(text)
    m3164 = RFC3164_RE.match(text)

    if m5424:
        g = m5424.groupdict()
        pri = int(g["pri"])
        facility, severity = _decode_pri(pri)
        try:
            device_time = datetime.fromisoformat(g["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            device_time = received_at
        return {
            "received_at": received_at,
            "device_time": device_time,
            "device_ip": src_ip,
            "source_port": src_port,
            "facility": facility,
            "severity": severity,
            "hostname": g["hostname"],
            "process_name": g["app"],
            "pid": int(g["procid"]) if g["procid"].isdigit() else 0,
            "message": g["message"],
            "raw_message": text,
        }

    if m3164:
        g = m3164.groupdict()
        pri = int(g["pri"])
        facility, severity = _decode_pri(pri)
        device_time = _parse_rfc3164_time(g["timestamp"], received_at)
        return {
            "received_at": received_at,
            "device_time": device_time,
            "device_ip": src_ip,
            "source_port": src_port,
            "facility": facility,
            "severity": severity,
            "hostname": g["hostname"],
            "process_name": (g["tag"] or "").strip(),
            "pid": int(g["pid"]) if g["pid"] else 0,
            "message": g["message"],
            "raw_message": text,
        }

    # Format match nahi hua - fallback: raw ko as-is store karo,
    # taake koi log silently gum na ho jaye.
    return {
        "received_at": received_at,
        "device_time": received_at,
        "device_ip": src_ip,
        "source_port": src_port,
        "facility": 0,
        "severity": 0,
        "hostname": src_ip,
        "process_name": "unknown",
        "pid": 0,
        "message": text,
        "raw_message": text,
    }


# A deliberately strict, documented key=value NAT format. Unrecognised tokens
# remain raw Events: never discard information merely because a log says NAT.
NAT_KEYS = {'private_ip', 'private_port', 'public_ip', 'public_port',
            'destination_ip', 'destination_port', 'protocol', 'subscriber_id'}
NAT_COLUMNS = ['timestamp', 'received_at', 'record_id', 'router_ip', 'private_ip',
               'private_port', 'public_ip', 'public_port', 'destination_ip',
               'destination_port', 'protocol', 'subscriber_id']
EVENT_COLUMNS = ['timestamp', 'received_at', 'record_id', 'router_ip', 'source_port',
                 'hostname', 'facility', 'severity', 'event_type', 'message', 'raw_message']

def route_syslog(raw, src_ip, src_port, received_at):
    legacy = parse_syslog(raw, src_ip, src_port, received_at)
    stamp = legacy['device_time']
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    base = dict(timestamp=stamp.astimezone(timezone.utc), received_at=received_at,
                record_id=str(uuid.uuid4()), router_ip=src_ip)
    message = legacy['message']
    # Only compact a complete bare NAT payload. Syslog envelopes contain extra
    # metadata, so preserve them as Events until a vendor-specific mapping exists.
    if legacy['process_name'] == 'unknown' and message.startswith('NAT '):
        try:
            tokens = message[4:].split()
            pairs = [token.split('=', 1) for token in tokens]
            fields = dict(pairs)
            if len(fields) != len(pairs) or set(fields) != NAT_KEYS:
                raise ValueError('Incomplete or additional NAT fields')
            for key in ('private_ip', 'public_ip', 'destination_ip'):
                fields[key] = str(ipaddress.IPv4Address(fields[key]))
            for key in ('private_port', 'public_port', 'destination_port'):
                value = int(fields[key])
                if not 0 <= value <= 65535:
                    raise ValueError('Invalid port')
                fields[key] = value
            if fields['protocol'].lower() not in ('tcp', 'udp'):
                raise ValueError('Unsupported protocol')
            fields['protocol'] = fields['protocol'].lower()
            return 'nat_sessions', {**base, **fields}, False
        except (ValueError, TypeError):
            pass
    category = 'system'
    lower = message.lower()
    for key in ('pppoe', 'dhcp', 'radius', 'ipsec', 'authentication', 'error', 'warning'):
        if key in lower:
            category = key
            break
    if message.startswith('NAT '):
        category = 'nat_unparsed'
    base.update(source_port=src_port, hostname=legacy['hostname'],
                facility=min(legacy['facility'], 23), severity=legacy['severity'],
                event_type=category, message=message, raw_message=raw.decode('utf-8', errors='replace'))
    return 'events', base, legacy['process_name'] == 'unknown'
