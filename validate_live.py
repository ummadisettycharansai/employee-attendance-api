"""Validate against an isolated real MongoDB database (never the production DB)."""
import os
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

os.environ['MONGO_URI'] = 'mongodb://127.0.0.1:27018'
os.environ['MONGO_DB'] = 'sdt_live_validation'
from fastapi.testclient import TestClient
from app import main
import sample_seed


def run():
    report = {'mongodb_version': main.client.server_info()['version'], 'checks': []}
    sample_seed.main()
    before = list(main.db.attendance_logs.find())
    sample_seed.main()
    assert before == list(main.db.attendance_logs.find()), 'Seed changed existing records'
    report['checks'].append('seed rerun preserves existing records')
    with TestClient(main.app) as client:
        def get(path, params=None):
            response = client.get(path, params=params)
            assert response.status_code == 200, (path, response.status_code, response.text)
            return response.json()

        report['health'] = get('/health')
        params = {'month': '2026-07'}
        report['monthly'] = get('/analytics/employees/EMP0001/monthly', params)
        report['summary'] = get('/analytics/departments/summary', params)
        report['leaderboard'] = get('/analytics/leaderboard/late', params)
        trend_params = {'from': '2026-07-01', 'to': '2026-07-31'}
        report['trend'] = get('/analytics/departments/Engineering/trend', trend_params)
        assert len(report['trend']['items']) == 31
        employees = list(main.db.employees.find({'department': 'Engineering'}))
        logs = list(main.db.attendance_logs.find())
        monthly_logs = [r for r in logs if r['date'].startswith('2026-07')]
        def present(records):
            return sum(0.5 if r.get('half_day') else 1 for r in records
                       if r['status'] in main.PRESENCE_STATUS and date.fromisoformat(r['date']).weekday() < 5)
        own_logs = [r for r in monthly_logs if r['emp_code'] == 'EMP0001']
        assert report['monthly']['present_days'] == present(own_logs)
        assert report['monthly']['total_late_minutes'] == sum(r.get('late_minutes', 0) for r in own_logs)
        all_employees = list(main.db.employees.find())
        for row in report['summary']['items']:
            eligible = [e for e in all_employees if e['department'] == row['department'] and e['joined_on'] <= '2026-07-31']
            codes = {e['emp_code'] for e in eligible}
            records = [r for r in monthly_logs if r['emp_code'] in codes]
            assert row['headcount'] == len(eligible)
            assert row['present_days'] == present(records)
            assert row['total_late_minutes'] == sum(r.get('late_minutes', 0) for r in records)
            hours = [Decimal(str(r['work_hours'])) for r in records if r['status'] in main.PRESENCE_STATUS and r.get('work_hours') is not None]
            expected_avg = float((sum(hours)/len(hours)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if hours else None
            assert row['avg_work_hours'] == expected_avg
        totals = {e['emp_code']: sum(r.get('late_minutes', 0) for r in monthly_logs if r['emp_code'] == e['emp_code']) for e in all_employees}
        for row in report['leaderboard']['items']:
            assert row['total_late_minutes'] == totals[row['emp_code']]
            assert row['rank'] == 1 + sum(total > row['total_late_minutes'] for total in totals.values())
        report['checks'].append('monthly, department totals, weighted work-hour means and leaderboard ranks match independent calculations')
        for row in report['trend']['items']:
            assert row['headcount'] == sum(e['joined_on'] <= row['date'] for e in employees)
            codes = {e['emp_code'] for e in employees}
            daily = [r for r in logs if r['date'] == row['date'] and r['emp_code'] in codes]
            expected = sum((0.5 if r.get('half_day') else 1) for r in daily if r['status'] in main.PRESENCE_STATUS)
            assert row['present_count'] == expected, row
        report['checks'].append('all analytics execute; trend dates, headcounts and presence match source records')
        report['explain'] = {}
        for endpoint, query in [
            ('attendance_list', {'emp_code': 'EMP0001'}),
            ('employee_monthly', dict(params, emp_code='EMP0001')),
            ('department_summary', params),
            ('late_leaderboard', params),
            ('department_trend', dict(trend_params, department='Engineering')),
        ]:
            report['explain'][endpoint] = get('/admin/explain/' + endpoint, query)['explain']
        assert 'IXSCAN' in json.dumps(report['explain']['attendance_list'])
        report['checks'].append('all five live explain commands execute; employee attendance query uses IXSCAN')
        emp_code = 'LIVE_CONCURRENCY'
        main.db.employees.update_one({'emp_code': emp_code}, {'$setOnInsert': {
            'emp_code': emp_code, 'name': 'Validation', 'email': 'validation@example.com',
            'department': 'Validation', 'shift_start': '09:30', 'shift_end': '18:30',
            'joined_on': '2026-01-01', 'created_at': datetime.now(timezone.utc),
        }}, upsert=True)
        day = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)
        payload = {'emp_code': emp_code, 'punched_at': int(day.timestamp()*1000), 'status': 'PRESENT'}
        # Only this synthetic record in the explicitly isolated validation database.
        main.db.attendance_logs.delete_many({'emp_code': emp_code})
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: client.post('/attendance/punch-in', json=payload).status_code, range(8)))
        assert sorted(statuses) == [201] + [409]*7, statuses
        payload.pop('status')
        payload['punched_at'] += 9*60*60*1000
        with ThreadPoolExecutor(max_workers=8) as pool:
            out_statuses = list(pool.map(lambda _: client.post('/attendance/punch-out', json=payload).status_code, range(8)))
        assert sorted(out_statuses) == [200] + [409]*7, out_statuses
        report['concurrency'] = {'punch_in': statuses, 'punch_out': out_statuses}
        report['checks'].append('eight concurrent punch-ins and punch-outs each produce exactly one success')
        report['indexes'] = {name: main.db[name].index_information() for name in ('employees', 'attendance_logs')}
    Path('live_validation_results.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'mongodb_version': report['mongodb_version'], 'checks': report['checks']}, indent=2))


if __name__ == '__main__':
    run()
