"""Storage arithmetic, not a throughput certification. Decimal TB throughout."""
import argparse
import json
import math


def estimate(eps, bytes_per_row, days=365, budget_tb=4):
    if not all(math.isfinite(v) and v > 0 for v in (eps, bytes_per_row, days, budget_tb)):
        raise ValueError('Inputs must be positive finite numbers')
    budget = budget_tb * 1e12
    annual = eps * 86400 * days * bytes_per_row
    return dict(average_logs_per_second=eps, compressed_bytes_per_row=bytes_per_row,
        retention_days=days, records_per_day=eps*86400,
        compressed_tb_for_retention=annual/1e12, budget_tb=budget_tb,
        budget_percent=100*annual/budget,
        maximum_average_eps_for_budget=budget/(86400*days*bytes_per_row),
        required_bytes_per_row_for_budget=budget/(86400*days*eps),
        note='Sizing estimate using supplied average rate and bytes/row. Excludes indexes, backups, replicas, spool and merge headroom. Does not demonstrate achievable ingestion throughput.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--eps',type=float,required=True,help='Sustained average, not peak')
    parser.add_argument('--bytes-per-row',type=float,required=True)
    parser.add_argument('--days',type=int,default=365)
    parser.add_argument('--budget-tb',type=float,default=4)
    args=parser.parse_args()
    try:
        print(json.dumps(estimate(args.eps,args.bytes_per_row,args.days,args.budget_tb),indent=2))
    except ValueError as e:
        parser.error(str(e))
