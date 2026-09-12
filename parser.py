import os
import base64
import re
from record_ids import next_id, as_id
import ipaddress

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# ============================================================
# TIMEZONE
# ============================================================
#
# RFC3164 timestamps do NOT contain a timezone.
#
# If MikroTik/router clocks are Pakistan local time, set:
#
#   SYSLOG_RFC3164_TIMEZONE=Asia/Karachi
#
# Otherwise UTC is used.
#
RFC3164_TIMEZONE_NAME = os.getenv(
    "SYSLOG_RFC3164_TIMEZONE",
    "UTC",
)

try:
    RFC3164_TIMEZONE = ZoneInfo(RFC3164_TIMEZONE_NAME)
except Exception:
    RFC3164_TIMEZONE = timezone.utc


# ============================================================
# SYSLOG REGEX
# ============================================================

# RFC3164:
#
# <PRI>MMM DD HH:MM:SS HOSTNAME TAG: MESSAGE
#
# Also supports common MikroTik form where TAG may not be
# followed by a colon.
#
RFC3164_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>"
    r"(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<tag>[^\s:\[]+)"
    r"(?:\[(?P<pid>\d+)\])?"
    r"(?::\s*|\s+)"
    r"(?P<message>.*)$",
    re.DOTALL,
)


# RFC5424:
#
# <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG
#
RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>"
    r"(?P<version>\d+)\s+"
    r"(?P<timestamp>\S+)\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<app>\S+)\s+"
    r"(?P<procid>\S+)\s+"
    r"(?P<msgid>\S+)\s+"
    r"(?P<sd>(?:\[.*?\]|-))"
    r"(?:\s(?P<message>.*))?$",
    re.DOTALL,
)


MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


# ============================================================
# SYSLOG HELPERS
# ============================================================

def _normalise_received_at(received_at: datetime) -> datetime:
    """
    ClickHouse timestamps are stored in UTC.

    If the listener accidentally supplies a naive datetime,
    treat it as UTC rather than letting timezone conversion
    behave unpredictably.
    """

    if received_at.tzinfo is None:
        received_at = received_at.replace(
            tzinfo=timezone.utc
        )

    return received_at.astimezone(timezone.utc)


def _decode_pri(pri: int):
    facility = pri // 8
    severity = pri % 8
    return facility, severity


def _parse_rfc3164_time(
    ts: str,
    received_at: datetime,
) -> datetime:
    """
    Parse the timestamp INSIDE the syslog packet.

    RFC3164 contains:
        month
        day
        HH:MM:SS

    but does NOT contain:
        year
        timezone

    The configured router timezone is therefore applied,
    then the timestamp is converted to UTC for storage.

    Year is selected based on the date closest to the
    packet receive time. This handles Dec -> Jan rollover.
    """

    received_at = _normalise_received_at(
        received_at
    )

    mon_str, day_str, time_str = ts.split(
        None,
        2,
    )

    month = MONTHS.get(mon_str)

    if month is None:
        return received_at

    try:
        day = int(day_str)

        hh, mm, ss = (
            int(x)
            for x in time_str.split(":")
        )

    except (ValueError, TypeError):
        return received_at

    local_receive = received_at.astimezone(
        RFC3164_TIMEZONE
    )

    candidates = []

    for year in (
        local_receive.year - 1,
        local_receive.year,
        local_receive.year + 1,
    ):
        try:
            candidate = datetime(
                year,
                month,
                day,
                hh,
                mm,
                ss,
                tzinfo=RFC3164_TIMEZONE,
            )

            candidates.append(candidate)

        except ValueError:
            continue

    if not candidates:
        return received_at

    # Pick the year whose date is closest to receive time.
    device_local = min(
        candidates,
        key=lambda candidate: abs(
            (
                candidate - local_receive
            ).total_seconds()
        ),
    )

    return device_local.astimezone(
        timezone.utc
    )


def _parse_rfc5424_time(
    value: str,
    received_at: datetime,
) -> datetime:
    """
    RFC5424 normally carries a full ISO timestamp.

    Example:
        2026-09-11T17:30:20+05:00
        2026-09-11T12:30:20Z

    This is the preferred packet timestamp.
    """

    received_at = _normalise_received_at(
        received_at
    )

    if not value or value == "-":
        return received_at

    try:
        parsed = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            # RFC5424 timestamp should normally contain a
            # timezone. If missing, use configured router
            # timezone rather than server receive time.
            parsed = parsed.replace(
                tzinfo=RFC3164_TIMEZONE
            )

        return parsed.astimezone(
            timezone.utc
        )

    except ValueError:
        return received_at


# ============================================================
# GENERIC SYSLOG PARSER
# ============================================================

def parse_syslog(
    raw: bytes,
    src_ip: str,
    src_port: int,
    received_at: datetime,
):
    """
    Parse the syslog envelope.

    IMPORTANT:

        device_time
            = timestamp carried by the router/syslog packet

        received_at
            = timestamp when our server received the UDP packet

    device_time is preferred for the searchable log timestamp.

    received_at is ONLY used as fallback when the packet does
    not contain a usable timestamp.

    No message is silently discarded.
    """

    received_at = _normalise_received_at(
        received_at
    )

    try:
        text = raw.decode(
            "utf-8",
            errors="replace",
        ).strip()

    except Exception:
        text = str(raw)

    # --------------------------------------------------------
    # RFC5424 first
    # --------------------------------------------------------

    m5424 = RFC5424_RE.match(text)

    if m5424:
        g = m5424.groupdict()

        try:
            pri = int(g["pri"])
        except (ValueError, TypeError):
            pri = 0

        facility, severity = _decode_pri(pri)

        device_time = _parse_rfc5424_time(
            g["timestamp"],
            received_at,
        )

        procid = g.get("procid") or ""

        return {
            "received_at": received_at,

            # Actual packet timestamp:
            "device_time": device_time,

            "device_ip": src_ip,
            "source_port": src_port,

            "facility": facility,
            "severity": severity,

            "hostname": g.get("hostname") or src_ip,

            "process_name": (
                g.get("app")
                if g.get("app") != "-"
                else ""
            ),

            "pid": (
                int(procid)
                if procid.isdigit()
                else 0
            ),

            "message": g.get("message") or "",

            "raw_message": text,
        }

    # --------------------------------------------------------
    # RFC3164
    # --------------------------------------------------------

    m3164 = RFC3164_RE.match(text)

    if m3164:
        g = m3164.groupdict()

        try:
            pri = int(g["pri"])
        except (ValueError, TypeError):
            pri = 0

        facility, severity = _decode_pri(pri)

        # IMPORTANT:
        # This comes from the timestamp INSIDE the packet.
        device_time = _parse_rfc3164_time(
            g["timestamp"],
            received_at,
        )

        return {
            "received_at": received_at,

            # Actual packet timestamp:
            "device_time": device_time,

            "device_ip": src_ip,
            "source_port": src_port,

            "facility": facility,
            "severity": severity,

            "hostname": g.get("hostname") or src_ip,

            "process_name": (
                g.get("tag") or ""
            ).strip(),

            "pid": (
                int(g["pid"])
                if g.get("pid")
                else 0
            ),

            "message": g.get("message") or "",

            "raw_message": text,
        }

    # --------------------------------------------------------
    # Unknown/no syslog envelope
    # --------------------------------------------------------
    #
    # Packet does not contain a timestamp we can reliably
    # extract.
    #
    # Therefore receive time is the ONLY safe fallback.
    #
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


# ============================================================
# NAT TABLE DEFINITIONS
# ============================================================

NAT_KEYS = {
    "private_ip",
    "private_port",
    "public_ip",
    "public_port",
    "destination_ip",
    "destination_port",
    "protocol",
    "subscriber_id",
}


NAT_COLUMNS = [
    "timestamp",
    "received_at",
    "record_id",
    "router_ip",

    "private_ip",
    "private_port",

    "public_ip",
    "public_port",

    "destination_ip",
    "destination_port",

    "protocol",
    "subscriber_id",
]


EVENT_COLUMNS = [
    "timestamp",
    "received_at",
    "record_id",
    "router_ip",
    "source_port",

    "hostname",
    "facility",
    "severity",

    "event_type",

    "message",
    "raw_message",
]


# Persist all diagnostic fields. Old spool rows receive explicit defaults.
NAT_V2_DEFAULTS = dict(source_port=0, input_interface='', output_interface='',
    connection_state='', tcp_flags='', packet_length=0, syslog_prefix='', record_type='', field_mask=63, raw_message='', raw_bytes_b64='',
    parse_status='legacy', application='', migration_source='')
NAT_V2_COLUMNS = NAT_COLUMNS + list(NAT_V2_DEFAULTS)


# ============================================================
# MIKROTIK NAT / SNAT PARSER
# ============================================================

IP_PORT = (
    r"(?P<{name}_ip>\d{{1,3}}(?:\.\d{{1,3}}){{3}})"
    r":(?P<{name}_port>\d{{1,5}})"
)


MIKROTIK_SNAT = re.compile(
    r"^(?P<prefix>.*?)"

    r"\bin:"
    r"(?P<input><[^>]+>|[^\s,]+)"
    r"\s+"

    r"out:"
    r"(?P<output>[^\s,]+)"
    r",\s*"

    r"(?P<metadata>.*?)\bproto\s+"
    r"(?P<protocol>TCP|UDP)"

    r"(?:\s+\("
    r"(?P<flags>[A-Z,\s]+)"
    r"\))?"

    r",\s*"

    + IP_PORT.format(name="private")

    + r"\s*->\s*"

    + IP_PORT.format(name="destination")

    + r",\s*NAT\s*\(\s*"

    + IP_PORT.format(name="translated_from")

    + r"\s*->\s*"

    + IP_PORT.format(name="public")

    + r"\s*\)"

    r"\s*->\s*"

    + IP_PORT.format(name="translated_destination")

    # MikroTik may include packet priority before len, for example:
    #   , prio 7->0, len 52
    # Keep it optional because many routers omit it.
    + r"(?:,\s*prio\s+\d+\s*->\s*\d+)?"

    + r"(?:,\s*len\s+"
    r"(?P<length>\d+))?"

    r"\s*$",

    re.DOTALL | re.IGNORECASE,
)


def _endpoint(
    match,
    name: str,
):
    ip_value = match[
        f"{name}_ip"
    ]

    port_value = match[
        f"{name}_port"
    ]

    ip_value = str(
        ipaddress.IPv4Address(
            ip_value
        )
    )

    port_value = int(port_value)

    if not 0 <= port_value <= 65535:
        raise ValueError(
            f"Invalid {name} port"
        )

    return ip_value, port_value


def mikrotik_snat(message):
    """
    Parse MikroTik firewall SNAT message.

    IMPORTANT:
    This function receives the SYSLOG MESSAGE BODY,
    not the complete syslog envelope.

    Example input:

      firewall,info forward: in:<pppoe-S-jameel>
      out:vlan2436, connection-state:new,snat proto TCP
      (ACK,RST),
      100.68.180.201:60734->57.144.149.32:443,
      NAT
      (100.68.180.201:60734->103.125.177.119:60734)
      ->57.144.149.32:443, len 52
    """

    if isinstance(message, bytes):
        text = message.decode(
            "utf-8",
            errors="strict",
        ).strip()
    else:
        text = str(message).strip()

    match = MIKROTIK_SNAT.fullmatch(
        text
    )

    if not match:
        return None

    metadata = match['metadata']
    if re.search(r'\bdnat\b', metadata, re.IGNORECASE):
        return None  # Combined SNAT/DNAT needs a separate, lossless mapping.
    state_match = re.search(r'\bconnection-state:([\w-]+)', metadata, re.IGNORECASE)
    state = state_match[1] if state_match else ''
    # Keep source MAC, unknown diagnostic attributes and envelope in the small
    # prefix field rather than dropping information or duplicating the NAT body.
    remainder = re.sub(r'\bconnection-state:[\w-]+,?\s*|\bsnat\b', '', metadata, flags=re.IGNORECASE).strip(' ,')
    private = _endpoint(
        match,
        "private",
    )

    destination = _endpoint(
        match,
        "destination",
    )

    translated_from = _endpoint(
        match,
        "translated_from",
    )

    public = _endpoint(
        match,
        "public",
    )

    translated_destination = _endpoint(
        match,
        "translated_destination",
    )

    # Router log must describe one internally-consistent
    # translation.
    if private != translated_from:
        raise ValueError(
            "Inconsistent NAT source translation"
        )

    if destination != translated_destination:
        raise ValueError(
            "Inconsistent NAT destination"
        )

    length_text = match["length"]

    packet_length = (
        int(length_text)
        if length_text
        else 0
    )

    if not 0 <= packet_length <= 65535:
        raise ValueError(
            "Invalid IPv4 packet length"
        )

    protocol = (
        match["protocol"]
        or ""
    ).lower()

    raw_flags = (
        match["flags"]
        or ""
    )

    flags = ",".join(
        flag.strip().upper()
        for flag in raw_flags.split(",")
        if flag.strip()
    )

    valid_flags = {
        "",
        "FIN",
        "SYN",
        "RST",
        "PSH",
        "ACK",
        "URG",
        "ECE",
        "CWR",
    }

    if (
        set(flags.split(","))
        - valid_flags
    ):
        raise ValueError(
            "Unknown TCP flags"
        )

    if protocol == "udp" and flags:
        raise ValueError(
            "TCP flags found on UDP packet"
        )

    interface = (
        match["input"]
        or ""
    ).strip("<>")

    # PPPoE interface identifies subscriber.
    #
    # DO NOT classify this as a PPPoE event when the log
    # contains a valid SNAT translation.
    subscriber = (
        interface
        if interface.lower().startswith(
            "pppoe-"
        )
        else ""
    )

    return {
        "private_ip": private[0],
        "private_port": private[1],

        "public_ip": public[0],
        "public_port": public[1],

        "destination_ip": destination[0],
        "destination_port": destination[1],

        "protocol": protocol,

        "subscriber_id": subscriber,

        "input_interface": (
            ""
            if subscriber
            else interface
        ),

        "output_interface": (
            match["output"] or ""
        ),

        "connection_state": (
            state
        ),

        "tcp_flags": flags,

        "packet_length": packet_length,

        "syslog_prefix": (
            (match["prefix"] or "") + remainder
        ).strip(),

        "record_type": "packet_snat",
    }


# ============================================================
# ROUTING
# ============================================================

# The six bits distinguish unknown defaults from genuinely observed zero values.
ENDPOINT_FIELDS = ('private_ip', 'private_port', 'public_ip', 'public_port', 'destination_ip', 'destination_port')
FIELD_BITS = {name: 1 << i for i, name in enumerate(ENDPOINT_FIELDS)}


def preserve_raw(raw):
    try:
        return raw.decode('utf-8'), ''
    except UnicodeDecodeError:
        return raw.decode('utf-8', errors='replace'), base64.b64encode(raw).decode('ascii')


def empty_record(raw, src_ip, src_port, received_at, stamp=None):
    text, binary = preserve_raw(raw)
    received_at = _normalise_received_at(received_at)
    row = dict(NAT_V2_DEFAULTS, timestamp=stamp or received_at, received_at=received_at,
        record_id=next_id(), router_ip=src_ip, protocol='', subscriber_id='',
        source_port=src_port, field_mask=0, raw_message=text, raw_bytes_b64=binary,
        parse_status='unknown', record_type='normalized_syslog')
    for key in ENDPOINT_FIELDS:
        row[key] = '0.0.0.0' if key.endswith('_ip') else 0
    return row


def strict_key_value(message):
    if not message.startswith('NAT '):
        return None
    pairs=[part.split('=',1) for part in message[4:].split()]
    fields=dict(pairs)
    if len(fields)!=len(pairs) or set(fields)!=NAT_KEYS:
        return None
    for key in ENDPOINT_FIELDS:
        fields[key] = str(ipaddress.IPv4Address(fields[key])) if key.endswith('_ip') else int(fields[key])
        if key.endswith('_port') and not 0 <= fields[key] <= 65535:
            return None
    if fields['protocol'].lower() not in ('tcp','udp'):
        return None
    fields['protocol']=fields['protocol'].lower()
    return fields


def fallback_normalize(row, message):
    # Only use labelled fields or the explicit source-NAT translation expression.
    # An arbitrary IP in an event is not assumed to be a private/public address.
    conflicts=set()
    def assign(key,value):
        if key in conflicts:
            return
        try:
            value=str(ipaddress.IPv4Address(value)) if key.endswith('_ip') else int(value)
            if key.endswith('_port') and not 0 <= value <= 65535:
                return
        except (ValueError,TypeError):
            return
        bit=FIELD_BITS[key]
        if row['field_mask'] & bit and row[key]!=value:
            row['field_mask'] &= ~bit
            row[key]='0.0.0.0' if key.endswith('_ip') else 0
            conflicts.add(key)
            return
        row[key]=value; row['field_mask'] |= bit

    aliases={'private_ip':'private_ip','private_port':'private_port','public_ip':'public_ip',
             'public_port':'public_port','destination_ip':'destination_ip','destination_port':'destination_port',
             'dest_ip':'destination_ip','dest_port':'destination_port'}
    for match in re.finditer(r'\b(private_ip|private_port|public_ip|public_port|destination_ip|destination_port|dest_ip|dest_port)\s*[=:]\s*([^\s,;]+)',message,re.I):
        assign(aliases[match[1].lower()],match[2])
    endpoint=r'(?P<{name}_ip>\d{{1,3}}(?:\.\d{{1,3}}){{3}})(?::(?P<{name}_port>\d+))?'
    translation=re.search(r'\bNAT\s*\(\s*'+endpoint.format(name='private')+r'\s*->\s*'+endpoint.format(name='public')+r'\s*\)\s*->\s*'+endpoint.format(name='destination'),message,re.I)
    if translation:
        for key in ENDPOINT_FIELDS:
            if translation[key] is not None:
                assign(key,translation[key])
    # PPPoE interfaces identify subscribers, not the event category.
    subscriber=re.search(r'\bin:\s*<?(pppoe-[^>\s,]+)>?',message,re.I)
    if subscriber:
        row['subscriber_id']=subscriber[1]
    else:
        user=re.search(r'\b(?:subscriber_id|username|user)\s*[=:]\s*[\"\']?([^\s,;\"\']+)',message,re.I)
        if user: row['subscriber_id']=user[1]
    protocol=re.search(r'\bproto(?:col)?\s*(?:[=:]\s*|\s+)([A-Za-z0-9_-]+)',message,re.I)
    if protocol: row['protocol']=protocol[1].lower()
    app=re.search(r'\b(?:app|application)\s*[=:]\s*([^\s,;]+)',message,re.I)
    if app: row['application']=app[1]
    row['parse_status']='partial' if row['field_mask'] or row['protocol'] or row['subscriber_id'] or row['application'] else 'unknown'
    return row


def normalize_syslog(raw, src_ip, src_port, received_at):
    row=empty_record(raw,src_ip,src_port,received_at)
    try:
        envelope=parse_syslog(raw,src_ip,src_port,received_at)
        row['timestamp']=_normalise_received_at(envelope['device_time'])
        translated=mikrotik_snat(raw)
        if translated is None:
            try:
                translated=strict_key_value(envelope['message'])
            except (ValueError,TypeError):
                translated=None
        if translated:
            row.update(translated,field_mask=63,parse_status='parsed')
            return row
    except (ValueError,TypeError,UnicodeError):
        pass
    # Use the full original text; extracted envelope fields must not hide clues.
    return fallback_normalize(row,row['raw_message'])


def route_syslog(raw, src_ip, src_port, received_at):
    try:
        row=normalize_syslog(raw,src_ip,src_port,received_at)
        return 'nat_sessions_v2',row,False
    except Exception:
        # Preserve even an unexpected parser exception. Listener exposes this
        # counter; it is a normalization failure, not a processing drop.
        row=empty_record(raw,src_ip,src_port,received_at)
        row['parse_status']='error'
        return 'nat_sessions_v2',row,True


def normalize_spooled(table, old):
    """Drain pre-upgrade spool batches into V2, preserving their stable UUIDs."""
    stamp=datetime.fromisoformat(old['timestamp']) if isinstance(old['timestamp'],str) else old['timestamp']
    received=datetime.fromisoformat(old['received_at']) if isinstance(old['received_at'],str) else old['received_at']
    if table=='events':
        raw=(old.get('raw_message') or old.get('message') or '').encode('utf-8')
        row=route_syslog(raw,old['router_ip'],old.get('source_port',0),received)[1]
        row['migration_source']='events_spool'
    elif table in ('nat_sessions','nat_sessions_v2'):
        row=dict(NAT_V2_DEFAULTS,**old)
    else:
        raise ValueError('Unsupported spool destination')
    row.update(timestamp=stamp,received_at=received,record_id=as_id(old['record_id']))
    return row
