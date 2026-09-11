"""Controlled, opt-in UDP generator. Does not approve any device."""
import argparse
import json
import socket
import time

def payload(i):
    return (f'NAT private_ip=100.64.{(i//254)%256}.{i%254+1} private_port={1024+i%64000} '
            f'public_ip=203.0.113.10 public_port={1024+i%64000} destination_ip=198.51.100.20 '
            f'destination_port=443 protocol=tcp subscriber_id=fixture-{i%10000}').encode()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=5514)
    parser.add_argument('--eps',type=int,default=1000);parser.add_argument('--seconds',type=int,default=10)
    parser.add_argument('--confirm-test-target',action='store_true')
    args=parser.parse_args()
    if not args.confirm_test_target:
        parser.error('Confirm this is an isolated test listener using --confirm-test-target')
    if not 1<=args.eps<=100000 or not 1<=args.seconds<=300:
        parser.error('EPS must be 1..100000 and seconds 1..300')
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    start=time.perf_counter();sent=0;failed=0
    for tick in range(args.seconds*100):
        target=(tick+1)*args.eps//100
        while sent+failed<target:
            try:
                sock.sendto(payload(sent+failed),(args.host,args.port));sent+=1
            except OSError:
                failed+=1
        time.sleep(max(0,start+(tick+1)/100-time.perf_counter()))
    elapsed=time.perf_counter()-start
    print(json.dumps(dict(sent=sent,send_failed=failed,seconds=elapsed,actual_send_eps=sent/elapsed,
                         note='Sent is not received. Compare listener/system snapshots before and after; UDP has no delivery acknowledgement.')))

if __name__=='__main__':
    main()
