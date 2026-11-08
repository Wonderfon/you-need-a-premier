"""Select bounded coordinator work; never run models or accept candidates."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def evaluate(usage, policy, now):
    try:
        used = float(usage['used_percent'])
        minutes = float(usage['window_minutes'])
        reset = float(usage['resets_at_unix'])
        anchor = stamp(policy['anchor_at_utc'])
        u0 = float(policy['anchor_used_percent'])
        ceiling = float(policy['ceiling_percent'])
        reserve = float(policy['shrink_percent'])
        tolerance = float(policy['tolerance_pp'])
        if not all(math.isfinite(x) for x in (used, minutes, reset, u0, ceiling, reserve, tolerance)):
            raise ValueError('nonfinite observation or policy')
        if not (0 <= used <= 100 and 0 <= u0 < reserve < ceiling <= 100 and 0 <= tolerance <= 10):
            raise ValueError('invalid thresholds')
        if minutes != policy['window_minutes'] or reset != policy['resets_at_unix']:
            raise ValueError('actual window changed; explicit new anchor required')
        age = (now - stamp(usage['observed_at_utc'])).total_seconds()
        end = datetime.fromtimestamp(reset, timezone.utc)
        if not (-60 <= age <= 900) or not (anchor <= now < end) or minutes <= 0:
            raise ValueError('stale observation or invalid window')
        baseline = u0 + (ceiling-u0)*(now-anchor)/(end-anchor)
        mode = ('hard_stop' if used >= ceiling else 'reserve' if used >= reserve
                else 'paced' if used > baseline+tolerance else 'normal')
        return dict(mode=mode, max_items=0 if mode == 'hard_stop' else 3 if mode == 'normal' else 1,
                    critical_only=mode == 'reserve', baseline_percent=round(baseline, 4),
                    reason=mode, safety_shutdown_allowed=True)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return dict(mode='unknown', max_items=0, critical_only=True,
                    reason=str(exc), safety_shutdown_allowed=True)


def build_queue(org, voice):
    # Existing authoritative queues retain completion state; this is a materialized view.
    items = {}
    def add(key, priority, created, source, kind):
        items[key] = dict(id=key, priority=priority, created=created or '',
                          source=source, kind=kind, critical=priority <= 1)
    for row in org['incidents']:
        if row['status'] not in ('resolved', 'cancelled'):
            add('incident:'+row['id'], 0, row.get('created'), row.get('evidence'), 'incident')
    for row in org['jobs']:
        if row['status'] in ('reviewing', 'blocked'):
            # An incident already supplies its disposition item; avoid duplicate review.
            if row.get('incident_id') and 'incident:'+row['incident_id'] in items:
                continue
            add('org:'+row['id'], 2, row.get('created'), row.get('source_ref'), 'org_review')
    for row in voice['tasks']['details']:
        if row.get('review_needed'):
            add('voice:'+row['id'], 1, row.get('created'), row.get('receipt'), 'voice_review')
    return sorted(items.values(), key=lambda item: (item['priority'], item['created'], item['id']))


def plan(usage, policy, org, voice, now):
    decision = evaluate(usage, policy, now)
    queue = build_queue(org, voice)
    end = stamp(voice['trial_window']['ends_at'])
    if now >= end:
        decision.update(mode='expired', max_items=0, reason='trial expired; perform shutdown only')
    selected = [item for item in queue if not decision['critical_only'] or item['critical']][:decision['max_items']]
    chosen = {item['id'] for item in selected}
    return dict(generated_at=now.isoformat(), budget=decision, selected=selected,
                deferred=[item for item in queue if item['id'] not in chosen],
                source_completion_authoritative=True, automatic_acceptance=False,
                model_calls=0)


def main():
    data = ROOT/'data'
    now = datetime.now(timezone.utc)
    try:
        read = lambda path: json.loads(path.read_text(encoding='utf-8'))
        result = plan(read(data/'codex-usage.json'), read(data/'codex-pacing-policy.json'),
                      read(data/'org/board.json'), read(data/'voice-trial/board.json'), now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = dict(generated_at=now.isoformat(), budget=dict(mode='unknown', max_items=0,
                      reason=str(exc), safety_shutdown_allowed=True), selected=[], deferred=[],
                      queue_unavailable=True, automatic_acceptance=False, model_calls=0)
    destination = data/'coordinator-review-board.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(destination)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
