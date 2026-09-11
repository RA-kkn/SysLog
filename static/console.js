'use strict';

const $ = id => document.getElementById(id);

let user = null,
    branding = {},
    nextCursor = null,
    activeParams = null,
    pageHistory = [],
    searchSequence = 0;

function notice(text) {
    $('notice').textContent = text;
    $('notice').style.display = 'block';
    setTimeout(() => $('notice').style.display = 'none', 6000);
}

async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };

    if (user) {
        headers['X-CSRF-Token'] = user.csrf;
    }

    const response = await fetch(path, { ...options, headers });

    if (!response.ok) {
        let data;

        try {
            data = await response.json();
        } catch {
            data = { detail: 'Request failed' };
        }

        if (response.status === 401) {
            $('console').hidden = true;
            $('login').hidden = false;
        }

        throw Error(
            typeof data.detail === 'string'
                ? data.detail
                : JSON.stringify(data.detail)
        );
    }

    return response.json();
}

function json(body) {
    return {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    };
}

function safe(action) {
    return async e => {
        if (e) e.preventDefault();

        try {
            await action(e);
        } catch (error) {
            notice(error.message);
        }
    };
}

function cell(tr, value, cls) {
    const td = document.createElement('td');
    td.textContent = value ?? '—';

    if (cls) {
        td.className = cls;
    }

    tr.append(td);
    return td;
}

function button(parent, label, action) {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = 'secondary';
    b.onclick = safe(action);
    parent.append(b);
}

function stats(id, items) {
    $(id).replaceChildren();

    for (const [label, value] of items) {
        const el = document.createElement('div');
        el.className = 'stat';

        const name = document.createElement('span');
        name.textContent = label;

        const v = document.createElement('strong');
        v.textContent = value ?? 'Unavailable';

        el.append(name, v);
        $(id).append(el);
    }
}

function table(id, rows, keys) {
    const t = document.createElement('table');
    const head = document.createElement('thead');
    const tr = document.createElement('tr');

    for (const key of keys) {
        const th = document.createElement('th');
        th.textContent = key.replaceAll('_', ' ');
        tr.append(th);
    }

    head.append(tr);
    t.append(head);

    const body = document.createElement('tbody');

    for (const row of rows) {
        const r = document.createElement('tr');

        for (const key of keys) {
            cell(r, row[key]);
        }

        body.append(r);
    }

    t.append(body);
    $(id).replaceChildren(t);
}

/*
 * IMPORTANT:
 * Backend timestamp should be the actual packet/syslog event timestamp.
 * This function only converts that timestamp into the configured
 * display timezone.
 */
function date(value) {
    if (!value) return '—';

    const parsed = new Date(value);

    if (Number.isNaN(parsed.getTime())) {
        return String(value);
    }

    const parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: branding.display_timezone || 'UTC',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23'
    }).formatToParts(parsed);

    const p = Object.fromEntries(
        parts.map(x => [x.type, x.value])
    );

    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

function bytes(value) {
    if (value == null) return 'Unavailable';
    if (value === 0) return '0 B';

    const n = Math.min(
        4,
        Math.floor(Math.log10(Math.max(1, value)) / 3)
    );

    return (
        (value / 1000 ** n).toFixed(2) +
        ' ' +
        ['B', 'KB', 'MB', 'GB', 'TB'][n]
    );
}

function num(value) {
    return value == null
        ? 'Unavailable'
        : Number(value).toLocaleString('en-US', {
              maximumFractionDigits: 2
          });
}

async function confirmAction(title, text, input = null) {
    $('confirmTitle').textContent = title;
    $('confirmText').textContent = text;
    $('confirmInput').hidden = input === null;
    $('confirmInput').value = input || '';

    $('confirm').showModal();

    return new Promise(resolve => {
        $('confirm').onclose = () =>
            resolve(
                $('confirm').returnValue === 'ok'
                    ? { value: $('confirmInput').value }
                    : null
            );
    });
}

async function loadBranding() {
    branding = await api('/api/settings/branding');

    document.querySelectorAll('.logo').forEach(e => {
        e.src = branding.logo_url;
    });

    document.querySelectorAll('.product').forEach(e => {
        e.textContent = branding.product_name;
    });

    document.title = branding.product_name;
    $('favicon').href = branding.favicon_url;

    document.documentElement.style.setProperty(
        '--accent',
        branding.accent_color
    );

    document.documentElement.style.setProperty(
        '--login-bg',
        `url("${branding.background_url}")`
    );

    $('timezone').textContent =
        'Display timezone: ' + branding.display_timezone;

    $('productName').value = branding.product_name;
    $('accent').value = branding.accent_color;
    $('displayTimezone').value = branding.display_timezone;
}

/*
 * Successful login/session:
 * open Log Search and AUTOMATICALLY load recent logs.
 */
async function session() {
    user = await api('/api/me');

    $('login').hidden = true;
    $('console').hidden = false;

    $('account').textContent =
        user.username + ' · ' + user.role;

    $('footerUser').textContent = user.role;

    document.querySelectorAll('.page').forEach(p => {
        p.hidden = p.id !== 'page-search';
    });

    document.querySelectorAll('[data-page]').forEach(b => {
        b.classList.toggle(
            'active',
            b.dataset.page === 'search'
        );
    });

    document.querySelectorAll('.admin').forEach(el => {
        el.hidden = user.role !== 'ADMIN';
    });

    $('export').hidden =
        user.role !== 'ADMIN' && !user.can_export;

    if (user.role !== 'ADMIN') {
        for (const id of ['users', 'runtime', 'workers', 'tables']) {
            $(id).replaceChildren();
        }
    }

    /*
     * AUTO LOAD:
     * No Search button click required.
     */
    await search();
}

$('loginForm').onsubmit = async e => {
    e.preventDefault();

    $('loginError').textContent = '';

    try {
        await api(
            '/api/login',
            json({
                username: $('username').value,
                password: $('password').value
            })
        );

        $('password').value = '';

        await session();
    } catch (err) {
        $('loginError').textContent = err.message;
    }
};

$('logout').onclick = safe(async () => {
    await api('/api/logout', { method: 'POST' });

    user = null;

    $('console').hidden = true;
    $('login').hidden = false;
});

/*
 * Navigation.
 *
 * When Log Search is opened again, refresh the logs automatically.
 */
document.querySelectorAll('[data-page]').forEach(btn => {
    btn.onclick = safe(async () => {
        document.querySelectorAll('.page').forEach(p => {
            p.hidden = true;
        });

        document.querySelectorAll('[data-page]').forEach(b => {
            b.classList.toggle('active', b === btn);
        });

        $('page-' + btn.dataset.page).hidden = false;

        if (btn.dataset.page === 'search') {
            await search();
        }

        if (btn.dataset.page === 'devices') {
            await devices();
        }

        if (btn.dataset.page === 'system') {
            await system();
        }

        if (btn.dataset.page === 'settings') {
            await settings();
        }
    });
});

function searchParams() {
    const p = new URLSearchParams({
        keyword: $('keyword').value.trim(),
        ip: $('router').value.trim(),
        kind: $('kind').value,
        limit: $('limit').value
    });

    const end = new Date();

    if ($('range').value === 'custom') {
        if (!$('start').value || !$('end').value) {
            throw Error('Choose start and end times');
        }

        p.set('start', $('start').value + 'Z');
        p.set('end', $('end').value + 'Z');
    } else {
        p.set('end', end.toISOString());

        p.set(
            'start',
            new Date(
                end.getTime() -
                    Number($('range').value) * 3600000
            ).toISOString()
        );
    }

    return p;
}

/*
 * NAT Sessions have their own structured table.
 *
 * Do NOT dump a successful NAT parse into one giant raw-message column.
 */
function renderLogs(rows, kind) {
    const natKeys = [
        'timestamp',
        'private_ip',
        'private_port',
        'public_ip',
        'public_port',
        'destination_ip',
        'destination_port',
        'protocol',
        'subscriber_id',
        'router_ip'
    ];

    let keys;

    if (kind === 'nat_sessions') {
        keys = natKeys;
    } else if (kind === 'events' || kind === 'legacy') {
        keys = [
            'timestamp',
            'router_ip',
            'event_type',
            'hostname',
            'severity',
            'message'
        ];
    } else {
        /*
         * Compact All Logs view.
         * NAT details remain available through Details.
         */
        keys = [
            'timestamp',
            'router_ip',
            'kind',
            'summary',
            'details'
        ];
    }

    const head = $('logHeader');
    head.replaceChildren();

    for (const key of keys) {
        const th = document.createElement('th');

        const labels = {
            timestamp: 'TIMESTAMP',
            private_ip: 'PRIVATE IP',
            private_port: 'PRIVATE PORT',
            public_ip: 'PUBLIC IP',
            public_port: 'PUBLIC PORT',
            destination_ip: 'DEST IP',
            destination_port: 'DEST PORT',
            protocol: 'PROTOCOL',
            subscriber_id: 'SUBSCRIBER ID',
            router_ip: 'ROUTER',
            event_type: 'EVENT TYPE',
            hostname: 'HOSTNAME',
            severity: 'SEVERITY',
            message: 'MESSAGE',
            kind: 'TYPE',
            summary: 'SUMMARY',
            details: 'DETAILS'
        };

        th.textContent =
            labels[key] || key.replaceAll('_', ' ').toUpperCase();

        head.append(th);
    }

    $('logs').replaceChildren();

    for (const row of rows || []) {
        const tr = document.createElement('tr');

        const nat =
            String(row.kind || '').startsWith('nat_sessions') ||
            (
                row.private_ip &&
                row.public_ip &&
                row.destination_ip
            );

        for (const key of keys) {
            if (key === 'details') {
                const td = cell(tr, '');

                const details = document.createElement('details');
                const summary = document.createElement('summary');

                summary.textContent = 'Details';

                details.append(summary);

                const dl = document.createElement('dl');

                for (const [name, value] of Object.entries(row)) {
                    if (
                        value === '' ||
                        value === null ||
                        value === undefined
                    ) {
                        continue;
                    }

                    const dt = document.createElement('dt');
                    const dd = document.createElement('dd');

                    dt.textContent = name;

                    dd.textContent =
                        typeof value === 'object'
                            ? JSON.stringify(value)
                            : String(value);

                    dl.append(dt, dd);
                }

                details.append(dl);
                td.append(details);
            }

            else if (key === 'summary') {
                if (nat) {
                    const subscriber =
                        row.subscriber_id || 'NAT';

                    const privateEndpoint =
                        `${row.private_ip || '—'}:${row.private_port ?? '—'}`;

                    const publicEndpoint =
                        `${row.public_ip || '—'}:${row.public_port ?? '—'}`;

                    const destination =
                        `${row.destination_ip || '—'}:${row.destination_port ?? '—'}`;

                    const protocol =
                        String(row.protocol || '').toUpperCase();

                    cell(
                        tr,
                        `${subscriber} · ${privateEndpoint} → ${publicEndpoint} → ${destination} ${protocol}`
                    );
                } else {
                    cell(tr, row.message || '—');
                }
            }

            else if (key === 'timestamp') {
                cell(tr, date(row[key]), 'endpoint');
            }

            else if (key === 'kind') {
                cell(
                    tr,
                    nat
                        ? 'NAT session'
                        : row.event_type || row.kind || 'Event'
                );
            }

            else if (key === 'protocol') {
                cell(
                    tr,
                    String(row[key] || '').toUpperCase()
                );
            }

            else {
                const endpoint =
                    key.includes('ip') ||
                    key.includes('port');

                cell(
                    tr,
                    row[key],
                    endpoint ? 'endpoint' : null
                );
            }
        }

        $('logs').append(tr);
    }
}

async function search(direction = 'fresh') {
    const previous = direction === 'previous';
    const next = direction === true;

    let p;

    if (previous) {
        p = new URLSearchParams(
            pageHistory[pageHistory.length - 1]
        );
    } else if (next) {
        p = new URLSearchParams(activeParams);
    } else {
        p = searchParams();
    }

    if (next && nextCursor) {
        p.set('cursor', nextCursor);
    }

    const sequence = ++searchSequence;
    const started = performance.now();

    $('searchStatus').textContent =
        'Loading recent logs...';

    $('next').disabled = true;
    $('previous').disabled = true;

    try {
        const data = await api('/api/search?' + p.toString());

        if (sequence !== searchSequence) {
            return;
        }

        if (next) {
            if (activeParams) {
                pageHistory.push(activeParams.toString());
            }
        } else if (previous) {
            pageHistory.pop();
        } else {
            pageHistory = [];
        }

        activeParams = p;
        nextCursor = data.next_cursor || null;

        renderLogs(
            Array.isArray(data.results) ? data.results : [],
            p.get('kind')
        );

        if (data.count) {
            $('searchStatus').textContent =
                `${data.count} records · ` +
                `${num(
                    data.query_ms ??
                    performance.now() - started
                )} ms · ` +
                `${date(data.start)} → ${date(data.end)}`;
        } else {
            $('searchStatus').textContent =
                $('range').value === '24' &&
                !p.get('keyword') &&
                !p.get('ip')
                    ? 'No logs found in the last 24 hours.'
                    : 'No logs match these filters.';
        }

        $('next').disabled = !nextCursor;
        $('previous').disabled = !pageHistory.length;

        if (data.notice) {
            $('searchStatus').textContent +=
                ' · ' + data.notice;
        }

    } catch (e) {
        if (sequence === searchSequence) {
            $('searchStatus').textContent =
                'Search failed: ' + e.message;

            $('logs').replaceChildren();

            $('next').disabled = !nextCursor;
            $('previous').disabled =
                !pageHistory.length;
        }
    }
}

/*
 * Manual Search still works.
 */
$('searchForm').onsubmit = safe(() => search());

$('next').onclick =
    safe(() => search(true));

$('previous').onclick =
    safe(() => search('previous'));

$('range').onchange = () => {
    $('customRange').hidden =
        $('range').value !== 'custom';
};

$('export').onclick = safe(async () => {
    const p = searchParams();

    const response = await fetch(
        '/api/export?' + p.toString()
    );

    if (!response.ok) {
        const e = await response.json();
        throw Error(e.detail);
    }

    const url = URL.createObjectURL(
        await response.blob()
    );

    const a = document.createElement('a');

    a.href = url;
    a.download = 'syslog-export.csv';

    a.click();

    setTimeout(
        () => URL.revokeObjectURL(url),
        1000
    );

    notice(
        'Export downloaded. Server row limit: ' +
        response.headers.get('X-Export-Row-Limit')
    );
});

async function devices() {
    const [counts, data] = await Promise.all([
        api('/api/devices/counts'),

        api(
            '/api/devices?' +
            new URLSearchParams({
                status: $('deviceStatus').value,
                q: $('deviceQuery').value
            })
        )
    ]);

    stats('deviceStats', [
        ['Approved', counts.approved],
        ['Pending approval', counts.pending],
        ['Blocked', counts.blocked],
        ['Total seen', counts.total]
    ]);

    $('devices').replaceChildren();

    for (const d of data.devices) {
        const tr = document.createElement('tr');

        cell(tr, d.ip, 'endpoint');

        const badge = document.createElement('span');

        badge.textContent = d.status;
        badge.className = 'badge ' + d.status;

        const td = document.createElement('td');
        td.append(badge);

        tr.append(td);

        cell(tr, d.name || '—');
        cell(tr, d.denied_attempts);
        cell(tr, date(d.first_seen));
        cell(tr, date(d.last_seen || d.last_attempt));
        cell(tr, d.applied_on_server);

        if (user.role === 'ADMIN') {
            const actions = cell(tr, '');
            actions.className = 'actions';

            for (const [label, action] of [
                ['Allow', 'approve'],
                ['Block', 'block']
            ]) {
                button(actions, label, async () => {
                    if (
                        !await confirmAction(
                            label + ' router',
                            label +
                            ' ' +
                            d.ip +
                            '? Historical records remain searchable.'
                        )
                    ) {
                        return;
                    }

                    await api(
                        '/api/devices/' +
                        d.ip +
                        '/' +
                        action,
                        { method: 'POST' }
                    );

                    await devices();
                });
            }

            button(actions, 'Rename', async () => {
                const result =
                    await confirmAction(
                        'Rename router',
                        d.ip,
                        d.name || ''
                    );

                if (result) {
                    await api(
                        '/api/devices/' +
                        d.ip +
                        '/name',
                        {
                            method: 'POST',
                            body: new URLSearchParams({
                                name: result.value
                            })
                        }
                    );

                    await devices();
                }
            });
        }

        $('devices').append(tr);
    }
}

$('refreshDevices').onclick =
    safe(devices);

$('deviceStatus').onchange =
    safe(devices);

$('deviceQuery').onchange =
    safe(devices);

$('approveForm').onsubmit =
    safe(async () => {
        await api(
            '/api/devices/add',
            {
                method: 'POST',
                body: new URLSearchParams({
                    ip: $('manualIP').value.trim()
                })
            }
        );

        $('manualIP').value = '';

        notice(
            'IP approved. Listener refresh: up to 5 seconds.'
        );

        await devices();
    });

async function system() {
    const d = await api('/api/system');

    stats('healthStats', [
        ['Listener', d.listener],
        ['ClickHouse', d.clickhouse],
        ['API / SQLite', d.api + ' / ' + d.sqlite],
        ['CPU', num(d.cpu_percent) + '%'],
        ['RAM used', bytes(d.ram.used)]
    ]);

    const s = d.storage;

    if (!s) {
        stats('storageStats', []);

        $('budgetText').textContent =
            d.clickhouse_error;

        $('tables').replaceChildren();
        $('budget').value = 0;
    } else {
        stats('ingestionStats', [
            [
                'Current insert EPS (complete minute)',
                num(s.current_eps)
            ],
            [
                '1h average EPS',
                num(s.eps_1h)
            ],
            [
                '24h average EPS',
                num(s.eps_24h)
            ],
            [
                '7d average EPS',
                num(s.eps_7d)
            ],
            [
                'Observed rows',
                num(s.observed_rows)
            ],
            [
                'Continuous observation (hours)',
                num(s.observed_seconds / 3600)
            ]
        ]);

        stats('compressionStats', [
            [
                'Database disk size',
                bytes(s.database_disk_bytes)
            ],
            [
                'Structured compressed size',
                bytes(s.database_compressed_bytes)
            ],
            [
                'Raw bytes / row',
                num(s.uncompressed_bytes_per_row)
            ],
            [
                'Compressed bytes / row',
                num(s.compressed_bytes_per_row)
            ],
            [
                'Compression ratio',
                num(s.compression_ratio) + '×'
            ],
            [
                'Measured net disk growth / day',
                s.measured_net_disk_growth_per_day == null
                    ? 'Unavailable'
                    : bytes(
                        s.measured_net_disk_growth_per_day
                    ) + '/day'
            ]
        ]);

        stats('storageStats', [
            [
                'Projection confidence',
                s.projection_confidence
            ],
            [
                'Projected daily compressed',
                bytes(
                    s.estimated_daily_compressed_bytes
                )
            ],
            [
                'Projected 30 days',
                bytes(s.estimated_30_day_bytes)
            ],
            [
                'Projected 365 days',
                bytes(s.estimated_365_day_bytes)
            ],
            [
                'Daily budget',
                bytes(s.daily_budget_bytes)
            ],
            [
                'Annual safety margin',
                s.annual_safety_margin_bytes == null
                    ? 'Unavailable'
                    : (
                        s.annual_safety_margin_bytes < 0
                            ? '-'
                            : ''
                    ) +
                    bytes(
                        Math.abs(
                            s.annual_safety_margin_bytes
                        )
                    )
            ],
            [
                'ClickHouse disk free',
                bytes(
                    s.disks.reduce(
                        (n, x) =>
                            n + x.free_bytes,
                        0
                    )
                )
            ],
            [
                'Days on free disk (projection)',
                num(
                    s.estimated_days_on_free_disk
                )
            ]
        ]);

        $('budget').value =
            s.budget_percent || 0;

        $('budgetText').textContent =
            num(s.budget_percent) +
            '% of ' +
            bytes(d.budget_bytes) +
            ' annual budget · Retention target: ' +
            d.retention_target_days +
            ' days' +
            (
                s.budget_percent > 100
                    ? ' · PROJECTED OVER BUDGET'
                    : ''
            );

        $('storageMethod').textContent =
            s.method +
            ' Observation: ' +
            num(s.observed_seconds / 3600) +
            ' hours.';

        table(
            'tables',
            s.tables,
            [
                'table',
                'rows',
                'uncompressed_bytes',
                'compressed_bytes',
                'disk_bytes'
            ]
        );
    }

    table(
        'workers',
        d.workers,
        [
            'pid',
            'up',
            'received',
            'queued',
            'parsed',
            'nat_parsed',
            'events_parsed',
            'inserted',
            'denied',
            'parse_failures',
            'write_failures',
            'queue_size',
            'queue_capacity',
            'dropped_processing',
            'statistics_failures',
            'dropped_queue',
            'dropped_spool',
            'spool_bytes',
            'insert_latency_ms'
        ]
    );
}

$('refreshSystem').onclick =
    safe(system);

async function settings() {
    await loadBranding();

    $('runtime').textContent =
        JSON.stringify(
            await api('/api/settings/system'),
            null,
            2
        );

    const data = await api('/api/users');

    table(
        'users',
        data.users,
        [
            'username',
            'full_name',
            'role',
            'enabled',
            'can_export'
        ]
    );

    const rows =
        $('users').querySelectorAll('tbody tr');

    data.users.forEach((u, i) => {
        const actions = cell(rows[i], '');
        actions.className = 'actions';

        button(actions, 'Edit', () => {
            $('userName').value = u.username;
            $('fullName').value = u.full_name;
            $('userRole').value = u.role;
            $('userEnabled').value =
                String(!!u.enabled);
            $('userExport').value =
                String(!!u.can_export);
            $('userPassword').value = '';
        });

        button(actions, 'Delete', async () => {
            if (
                await confirmAction(
                    'Delete user',
                    'Delete ' + u.username + '?'
                )
            ) {
                await api(
                    '/api/users',
                    json({
                        ...u,
                        enabled: !!u.enabled,
                        can_export: !!u.can_export,
                        delete: true
                    })
                );

                await settings();
            }
        });
    });
}

$('brandingForm').onsubmit =
    safe(async () => {
        await api(
            '/api/settings/branding',
            json({
                product_name:
                    $('productName').value,
                accent_color:
                    $('accent').value,
                display_timezone:
                    $('displayTimezone').value
            })
        );

        await loadBranding();
        notice('Branding saved');
    });

$('uploadImage').onclick =
    safe(async () => {
        const file =
            $('imageFile').files[0];

        if (!file) {
            throw Error('Select an image');
        }

        if (file.size > 2097152) {
            throw Error(
                'Maximum image size is 2 MiB'
            );
        }

        const body = new FormData();

        body.set(
            'kind',
            $('imageKind').value
        );

        body.set('file', file);

        await api(
            '/api/settings/branding/upload',
            {
                method: 'POST',
                body
            }
        );

        await loadBranding();

        notice('Image saved');
    });

$('userForm').onsubmit =
    safe(async () => {
        await api(
            '/api/users',
            json({
                username:
                    $('userName').value,
                full_name:
                    $('fullName').value,
                password:
                    $('userPassword').value,
                role:
                    $('userRole').value,
                enabled:
                    $('userEnabled').value ===
                    'true',
                can_export:
                    $('userExport').value ===
                    'true'
            })
        );

        $('userPassword').value = '';

        notice(
            'User saved; existing sessions revoked'
        );

        await settings();
    });

function theme() {
    const value =
        $('theme').value;

    localStorage.setItem(
        'theme',
        value
    );

    document.documentElement.dataset.theme =
        value === 'system'
            ? (
                matchMedia(
                    '(prefers-color-scheme: dark)'
                ).matches
                    ? 'dark'
                    : 'light'
            )
            : value;
}

$('theme').value =
    localStorage.getItem('theme') ||
    'system';

$('theme').onchange = theme;

matchMedia(
    '(prefers-color-scheme: dark)'
).addEventListener(
    'change',
    theme
);

theme();

/*
 * Application startup.
 *
 * 1. Load branding
 * 2. Restore session
 * 3. session() automatically calls search()
 *
 * Therefore refreshing the browser automatically
 * loads the latest logs.
 */
(async () => {
    try {
        await loadBranding();
        await session();
    } catch (error) {
        if (
            !error.message.includes(
                'Session expired'
            )
        ) {
            notice(error.message);
        }
    }
})();