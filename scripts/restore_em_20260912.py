import json, os, sys, collections
import psycopg

APPLY = '--apply' in sys.argv
repo = '/home/ubuntu/quant_trading'
backup = os.path.join(repo, 'run_artifacts/l00r_remediation_backup_1789188197.json')

dsn = None
for line in open(os.path.join(repo, '.env')):
    line = line.strip()
    if line.startswith('QUANT_AUDIT_PG_DSN='):
        dsn = line.split('=', 1)[1].strip().strip('"').strip(chr(39))
        break
if not dsn:
    sys.exit('DSN not found in .env')

d = json.load(open(backup))
em = d['experience_memory']
rt = {x['review_id']: x for x in d['revision_targets']}
po = {x['review_id']: x for x in d['pool_only_targets']}

cols = ['experience_id','trade_id','source_table','source_id','append_source','regime_id',
        'setup_hash','decision_context_json','outcome_label','reward_score','failure_tags_json',
        'recommended_action','evidence_strength','artifact_version','evolution_run_id','created_at']

plan = []
for row in em:
    sid = row['source_id']
    if sid in rt:
        new_int = rt[sid]['new_integrity']
    elif sid in po:
        new_int = po[sid]['attribution_integrity']
    else:
        new_int = None
    ctx = json.loads(row['decision_context_json'] or '{}')
    old = ctx.get('attribution_integrity')
    if new_int:
        ctx['attribution_integrity'] = new_int
    plan.append((row, old, new_int, json.dumps(ctx, ensure_ascii=False)))

print('rows in backup:', len(plan))
print('new_integrity applied:', dict(collections.Counter(p[2] for p in plan)))
print('old attribution_integrity:', dict(collections.Counter(p[1] for p in plan)))
print('would be learning-eligible after restore:', sum(1 for p in plan if str(p[2]).lower() == 'full'))
print()
for row, old, new_int, _ in plan[:3]:
    print('  ', row['experience_id'][:46], '|', old, '->', new_int)

if not APPLY:
    print()
    print('DRY RUN - no writes.')
    sys.exit(0)

conn = psycopg.connect(dsn)
cur = conn.cursor()
cur.execute('SELECT count(*) FROM runtime.experience_memory')
before = cur.fetchone()[0]
sql = 'INSERT INTO runtime.experience_memory (%s) VALUES (%s) ON CONFLICT (experience_id) DO NOTHING' % (
    ','.join(cols), ','.join(['%s'] * len(cols)))
n = 0
for row, old, new_int, new_ctx in plan:
    vals = [new_ctx if c == 'decision_context_json' else row.get(c) for c in cols]
    cur.execute(sql, vals)
    n += cur.rowcount
conn.commit()
cur.execute('SELECT count(*) FROM runtime.experience_memory')
after = cur.fetchone()[0]
cur.execute(chr(34) + 'x' + chr(34)) if False else None
cur.execute("SELECT count(*) FROM runtime.experience_memory WHERE decision_context_json::jsonb->>'attribution_integrity' = 'full'")
full = cur.fetchone()[0]
print()
print('inserted:', n, '| before:', before, '-> after:', after, '| integrity=full:', full)
conn.close()
