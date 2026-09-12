'use strict';

const $ = id => document.getElementById(id);

let user = null,
    branding = {},
    nextCursor = null,
    activeParams = null,
    pageHistory = [],
    searchSequence = 0;

/*
 * UI timestamps are ALWAYS displayed in Pakistan Standard Time.
 * ClickHouse/API timestamps remain UTC.
 */
const DISPLAY_TIMEZONE = 'Asia/Karachi';


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
 * API/ClickHouse DateTime64('UTC') can arrive without Z/offset.
 *
 * Example:
 *     2026-09-12T05:37:53.123000
 *
 * JavaScript would normally interpret that as browser-local time.
 * It is actually UTC, so append Z before parsing.
 */
function backendDate(value) {
    if (!value) return null;

    let text = String(value).trim();

    if (
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)
    ) {
        text += 'Z';
    }

    const parsed = new Date(text);

    return Number.isNaN(parsed.getTime())
        ? null
        : parsed;
}


/*
 * Display UTC backend timestamp as Pakistan time.
 */
function date(value) {
    if (!value) return '—';

    const parsed = backendDate(value);

    if (!parsed) {
        return String(value);
    }

    const parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: DISPLAY_TIMEZONE,
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


/*
 * datetime-local has no timezone.
 *
 * User enters Pakistan local time.
 * Convert Pakistan UTC+05:00 -> UTC before sending to API.
 */
function pakistanLocalToUtc(value) {
    if (!value) {
        throw Error('Choose start and end times');
    }

    const parsed = new Date(value + '+05:00');

    if (Number.isNaN(parsed.getTime())) {
        throw Error('Invalid Pakistan date/time');
    }

    return parsed.toISOString();
}


function bytes(value) {
    if (value == null) return 'Unavailable';
    if (value === 0) return '0 B';

    const n = Math.min(
        4,
        Math.floor(
            Math.log10(Math.max(1, value)) / 3
        )
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

    /*
     * Force Pakistan display timezone.
     */
    $('timezone').textContent =
        'Display timezone: ' + DISPLAY_TIMEZONE;

    $('productName').value =
        branding.product_name;

    $('accent').value =
        branding.accent_color;

    $('displayTimezone').value =
        DISPLAY_TIMEZONE;
}


async function session() {
    user = await api('/api/me');

    $('login').hidden = true;
    $('console').hidden = false;

    $('account').textContent =
        user.username + ' · ' + user.role;

    $('footerUser').textContent =
        user.role;

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
        user.role !== 'ADMIN' &&
        !user.can_export;

    if (user.role !== 'ADMIN') {
        for (const id of [
            'users',
            'runtime',
            'workers',
            'tables'
        ]) {
            $(id).replaceChildren();
        }
    }

    /*
     * Automatically load recent logs after login.
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
        $('loginError').textContent =
            err.message;
    }
};


$('logout').onclick = safe(async () => {
    await api('/api/logout', {
        method: 'POST'
    });

    user = null;

    $('console').hidden = true;
    $('login').hidden = false;
});


/*
 * Navigation
 */
document.querySelectorAll('[data-page]').forEach(btn => {
    btn.onclick = safe(async () => {

        document.querySelectorAll('.page').forEach(p => {
            p.hidden = true;
        });

        document.querySelectorAll('[data-page]').forEach(b => {
            b.classList.toggle(
                'active',
                b === btn
            );
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

        if (
            !$('start').value ||
            !$('end').value
        ) {
            throw Error(
                'Choose start and end times'
            );
        }

        /*
         * Custom form values are Pakistan local time.
         * Convert them to UTC for ClickHouse query.
         */
        p.set(
            'start',
            pakistanLocalToUtc(
                $('start').value
            )
        );

        p.set(
            'end',
            pakistanLocalToUtc(
                $('end').value
            )
        );

    } else {

        /*
         * Current browser time -> UTC ISO.
         */
        p.set(
            'end',
            end.toISOString()
        );

        p.set(
            'start',
            new Date(
                end.getTime() -
                Number(
                    $('range').value
                ) * 3600000
            ).toISOString()
        );
    }

    return p;
}

const natColumns = [
    ['private_ip','Private IP'], ['private_port','Private Port'],
    ['public_ip','Public IP'], ['public_port','Public Port'],
    ['destination_ip','Dest IP'], ['destination_port','Dest Port'],
    ['protocol','Protocol/App'], ['timestamp','Session Start Time'],
    ['subscriber_id','Subscriber/User ID']
];

function renderLogs(rows) {
    $('logHeader').replaceChildren(); $('logs').replaceChildren();
    for (const [,label] of natColumns) {
        const th=document.createElement('th'); th.textContent=label;
        $('logHeader').append(th);
    }
    for (const row of rows) {
        const tr=document.createElement('tr');
        for (const [key] of natColumns) {
            const value=key==='timestamp'?date(row[key]):row[key];
            cell(tr,value ?? '',key.includes('ip')||key.includes('port')||key==='timestamp'?'endpoint':null);
        }
        $('logs').append(tr);
    }
}

async function search(direction = 'fresh') {
    const previous =
        direction === 'previous';

    const next =
        direction === true;

    let p;

    if (previous) {

        p = new URLSearchParams(
            pageHistory[
                pageHistory.length - 1
            ]
        );

    } else if (next) {

        p = new URLSearchParams(
            activeParams
        );

    } else {

        p = searchParams();
    }

    if (
        next &&
        nextCursor
    ) {
        p.set(
            'cursor',
            nextCursor
        );
    }

    const sequence =
        ++searchSequence;

    const started =
        performance.now();

    $('searchStatus').textContent =
        'Loading recent logs...';

    $('next').disabled = true;
    $('previous').disabled = true;

    try {

        const data =
            await api(
                '/api/search?' +
                p.toString()
            );

        if (
            sequence !==
            searchSequence
        ) {
            return;
        }

        if (next) {

            if (activeParams) {
                pageHistory.push(
                    activeParams.toString()
                );
            }

        } else if (previous) {

            pageHistory.pop();

        } else {

            pageHistory = [];
        }

        activeParams = p;
        nextCursor =
            data.next_cursor || null;

        renderLogs(
            Array.isArray(data.results)
                ? data.results
                : [],
            p.get('kind')
        );

        if (data.count) {

            $('searchStatus').textContent =
                `${num(data.count)} shown of ` +
                `${num(data.total_count)} matching records · ` +
                `${num(
                    data.query_ms ??
                    performance.now() -
                    started
                )} ms · ` +
                `${date(data.start)} → ` +
                `${date(data.end)}`;

        } else {

            $('searchStatus').textContent =
                $('range').value === '24' &&
                !p.get('keyword') &&
                !p.get('ip')
                    ? 'No logs found in the last 24 hours.'
                    : 'No logs match these filters.';
        }

        $('next').disabled =
            !nextCursor;

        $('previous').disabled =
            !pageHistory.length;

        if (data.notice) {
            $('searchStatus').textContent +=
                ' · ' + data.notice;
        }

    } catch (e) {

        if (
            sequence ===
            searchSequence
        ) {
            $('searchStatus').textContent =
                'Search failed: ' +
                e.message;

            $('logs').replaceChildren();

            $('next').disabled =
                !nextCursor;

            $('previous').disabled =
                !pageHistory.length;
        }
    }
}


/*
 * Search button still works.
 */
$('searchForm').onsubmit =
    safe(() => search());

$('next').onclick =
    safe(() => search(true));

$('previous').onclick =
    safe(() =>
        search('previous')
    );

$('range').onchange = () => {
    $('customRange').hidden =
        $('range').value !==
        'custom';
};


$('export').onclick =
    safe(async () => {

        const p =
            searchParams();

        const response =
            await fetch(
                '/api/export?' +
                p.toString()
            );

        if (!response.ok) {
            const e =
                await response.json();

            throw Error(
                e.detail
            );
        }

        const url =
            URL.createObjectURL(
                await response.blob()
            );

        const a =
            document.createElement('a');

        a.href = url;
        a.download =
            'syslog-export.csv';

        a.click();

        setTimeout(
            () =>
                URL.revokeObjectURL(
                    url
                ),
            1000
        );

        notice(
            'Export downloaded. Server row limit: ' +
            response.headers.get(
                'X-Export-Row-Limit'
            )
        );
    });


async function devices() {
    const [counts, data] =
        await Promise.all([
            api(
                '/api/devices/counts'
            ),

            api(
                '/api/devices?' +
                new URLSearchParams({
                    status:
                        $('deviceStatus').value,
                    q:
                        $('deviceQuery').value
                })
            )
        ]);

    stats(
        'deviceStats',
        [
            ['Approved', counts.approved],
            ['Pending approval', counts.pending],
            ['Blocked', counts.blocked],
            ['Total seen', counts.total]
        ]
    );

    $('devices').replaceChildren();

    for (const d of data.devices) {

        const tr =
            document.createElement('tr');

        cell(
            tr,
            d.ip,
            'endpoint'
        );

        const badge =
            document.createElement('span');

        badge.textContent =
            d.status;

        badge.className =
            'badge ' + d.status;

        const td =
            document.createElement('td');

        td.append(badge);
        tr.append(td);

        cell(
            tr,
            d.name || '—'
        );

        cell(
            tr,
            d.denied_attempts
        );

        cell(
            tr,
            date(d.first_seen)
        );

        cell(
            tr,
            date(
                d.last_seen ||
                d.last_attempt
            )
        );

        cell(
            tr,
            d.applied_on_server
        );

        if (
            user.role === 'ADMIN'
        ) {

            const actions =
                cell(tr, '');

            actions.className =
                'actions';

            for (
                const [label, action]
                of [
                    ['Allow', 'approve'],
                    ['Block', 'block']
                ]
            ) {

                button(
                    actions,
                    label,
                    async () => {

                        if (
                            !await confirmAction(
                                label +
                                ' router',
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
                            {
                                method:
                                    'POST'
                            }
                        );

                        await devices();
                    }
                );
            }

            button(
                actions,
                'Rename',
                async () => {

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
                                method:
                                    'POST',

                                body:
                                    new URLSearchParams({
                                        name:
                                            result.value
                                    })
                            }
                        );

                        await devices();
                    }
                }
            );
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

                body:
                    new URLSearchParams({
                        ip:
                            $('manualIP')
                                .value
                                .trim()
                    })
            }
        );

        $('manualIP').value = '';

        notice(
            'IP approved. Listener refresh: up to 5 seconds.'
        );

        await devices();
    });


/*
 * System Health
 */
async function system() {
    const d =
        await api('/api/system');

    stats(
        'healthStats',
        [
            ['Listener', d.listener],
            ['ClickHouse', d.clickhouse],
            [
                'API / SQLite',
                d.api + ' / ' + d.sqlite
            ],
            [
                'CPU',
                num(d.cpu_percent) + '%'
            ],
            [
                'RAM',
                bytes(d.ram.used)
            ]
        ]
    );

    const s =
        d.storage;

    for (
        const id of [
            'ingestionStats',
            'compressionStats',
            'storageStats',
            'tables',
            'workers'
        ]
    ) {
        $(id).replaceChildren();
    }

    if (!s) {

        $('budgetText').textContent =
            d.clickhouse_error ||
            'Storage unavailable';

        $('storageMethod').textContent =
            '';

        $('budget').value = 0;

    } else {

        /*
         * IMPORTANT:
         *
         * total_rows = actual rows currently stored in
         * nat_sessions_v2 (including preserved raw messages).
         *
         * observed_rows is still retained by backend for
         * forecasting/measurement but is NOT presented as
         * database total.
         */
        stats(
            'ingestionStats',
            [
                [
                    'Insert EPS',
                    num(s.current_eps)
                ],
                [
                    'Total stored rows',
                    num(s.total_rows)
                ]
            ]
        );

        stats(
            'compressionStats',
            [
                [
                    'DB size',
                    bytes(
                        s.database_disk_bytes
                    )
                ],
                [
                    'Compression ratio',
                    s.compression_ratio == null
                        ? 'Unavailable'
                        : num(
                            s.compression_ratio
                          ) + 'x'
                ],
                [
                    'Daily net growth',
                    s.measured_net_disk_growth_per_day == null
                        ? 'Unavailable'
                        : (
                            s.measured_net_disk_growth_per_day < 0
                                ? '-'
                                : ''
                          ) +
                          bytes(
                              Math.abs(
                                  s.measured_net_disk_growth_per_day
                              )
                          ) +
                          '/day'
                ]
            ]
        );

        stats(
            'storageStats',
            [
                [
                    '30-day estimate',
                    bytes(
                        s.estimated_30_day_bytes
                    )
                ],
                [
                    '365-day estimate',
                    bytes(
                        s.estimated_365_day_bytes
                    )
                ],
                [
                    'Free disk',
                    bytes(
                        s.disks.reduce(
                            (n, x) =>
                                n +
                                x.free_bytes,
                            0
                        )
                    )
                ],
                [
                    'Estimated days remaining',
                    num(
                        s.estimated_days_on_free_disk
                    )
                ]
            ]
        );

        $('budget').value =
            s.budget_percent || 0;

        $('budgetText').textContent =
            `${num(s.budget_percent)}% of ` +
            `${bytes(d.budget_bytes)} annual budget | ` +
            `Retention: ${d.retention_target_days} days`;

        $('storageMethod').textContent =
            `Forecast confidence: ${s.projection_confidence}. ` +
            `Continuous history: ${num(
                s.observed_seconds / 3600
            )} hours. ` +
            `EPS uses complete minutes; estimates use actual stored bytes per row. ` +
            `Daily growth is net disk change normalized over ` +
            `${num(
                s.disk_growth_observation_seconds /
                3600
            )} hours.`;

        table(
            'tables',
            s.tables.map(t => ({
                table:
                    t.table,

                rows:
                    num(t.rows),

                disk:
                    bytes(
                        t.disk_bytes
                    ),

                compressed:
                    bytes(
                        t.compressed_bytes
                    ),

                ratio:
                    t.compressed_bytes
                        ? num(
                            t.uncompressed_bytes /
                            t.compressed_bytes
                          ) + 'x'
                        : 'Unavailable'
            })),
            [
                'table',
                'rows',
                'disk',
                'compressed',
                'ratio'
            ]
        );
    }

    for (const w of d.workers) {

        const card =
            document.createElement(
                'section'
            );

        card.className =
            'worker-card';

        const title =
            document.createElement(
                'h3'
            );

        title.textContent =
            `Worker ${w.pid} - ` +
            `${w.up ? 'Running' : 'Offline'}`;

        card.append(title);

        const grid =
            document.createElement(
                'div'
            );

        grid.className =
            'worker-grid';

        const entries = [
            ['Received', num(w.received)],
            ['Queued', num(w.queued)],
            ['NAT parsed', num(w.nat_parsed)],
            ['Fallback records', num(w.normalized_fallback)],
            ['Stored / acknowledged', num(w.inserted)],
            ['Unauthorized', num(w.denied)],
            [
                'Queue',
                `${num(w.queue_size)} / ${num(w.queue_capacity)}`
            ],
            ['Spool', bytes(w.spool_bytes)],
            ['Parse failures', num(w.parse_failures)],
            ['Write failures', num(w.write_failures)],
            ['Queue drops', num(w.dropped_queue)],
            ['Spool drops', num(w.dropped_spool)],
            [
                'Processing drops',
                num(
                    w.dropped_processing ??
                    0
                )
            ],
            [
                'Statistics failures',
                num(
                    w.statistics_failures ??
                    0
                )
            ]
        ];

        for (
            const [name, value]
            of entries
        ) {

            const item =
                document.createElement(
                    'div'
                );

            const label =
                document.createElement(
                    'span'
                );

            const val =
                document.createElement(
                    'strong'
                );

            label.textContent =
                name;

            val.textContent =
                value;

            item.append(
                label,
                val
            );

            grid.append(item);
        }

        card.append(grid);
        $('workers').append(card);
    }

    if (!d.workers.length) {
        $('workers').textContent =
            'No listener heartbeat available.';
    }
}


$('refreshSystem').onclick =
    safe(system);


async function settings() {
    await loadBranding();

    $('runtime').textContent =
        JSON.stringify(
            await api(
                '/api/settings/system'
            ),
            null,
            2
        );

    const data =
        await api('/api/users');

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
        $('users')
            .querySelectorAll(
                'tbody tr'
            );

    data.users.forEach((u, i) => {

        const actions =
            cell(
                rows[i],
                ''
            );

        actions.className =
            'actions';

        button(
            actions,
            'Edit',
            () => {

                $('userName').value =
                    u.username;

                $('fullName').value =
                    u.full_name;

                $('userRole').value =
                    u.role;

                $('userEnabled').value =
                    String(
                        !!u.enabled
                    );

                $('userExport').value =
                    String(
                        !!u.can_export
                    );

                $('userPassword').value =
                    '';
            }
        );

        button(
            actions,
            'Delete',
            async () => {

                if (
                    await confirmAction(
                        'Delete user',
                        'Delete ' +
                        u.username +
                        '?'
                    )
                ) {

                    await api(
                        '/api/users',
                        json({
                            ...u,

                            enabled:
                                !!u.enabled,

                            can_export:
                                !!u.can_export,

                            delete:
                                true
                        })
                    );

                    await settings();
                }
            }
        );
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

                /*
                 * Application display timezone is Pakistan.
                 */
                display_timezone:
                    DISPLAY_TIMEZONE
            })
        );

        await loadBranding();

        notice(
            'Branding saved'
        );
    });


$('uploadImage').onclick =
    safe(async () => {

        const file =
            $('imageFile').files[0];

        if (!file) {
            throw Error(
                'Select an image'
            );
        }

        if (
            file.size > 2097152
        ) {
            throw Error(
                'Maximum image size is 2 MiB'
            );
        }

        const body =
            new FormData();

        body.set(
            'kind',
            $('imageKind').value
        );

        body.set(
            'file',
            file
        );

        await api(
            '/api/settings/branding/upload',
            {
                method: 'POST',
                body
            }
        );

        await loadBranding();

        notice(
            'Image saved'
        );
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

        $('userPassword').value =
            '';

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
    localStorage.getItem(
        'theme'
    ) || 'system';

$('theme').onchange =
    theme;

matchMedia(
    '(prefers-color-scheme: dark)'
).addEventListener(
    'change',
    theme
);

theme();


/*
 * Startup:
 * 1. Load branding
 * 2. Restore session
 * 3. Automatically load recent logs
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
            notice(
                error.message
            );
        }
    }
})();