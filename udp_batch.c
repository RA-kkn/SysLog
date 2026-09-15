#define _GNU_SOURCE
#include <sys/socket.h>
#include <netinet/in.h>
#include <time.h>
#include <stdint.h>
#include <stdlib.h>
#include <errno.h>
#include <string.h>

/* Private wire header matches shared_packets.HEADER (!4sHIQ). */
#define STRIDE (65535 + 18)
struct batch {
    unsigned count;
    struct mmsghdr *messages;
    struct iovec *iov;
    struct sockaddr_in *addresses;
    unsigned char *data;
    unsigned *lengths;
};
void udp_batch_free(struct batch *b) {
    if (!b) return;
    free(b->messages); free(b->iov); free(b->addresses);
    free(b->data); free(b->lengths); free(b);
}
struct batch *udp_batch_new(unsigned count) {
    if (!count || count > 256) { errno=EINVAL; return NULL; }
    struct batch *b=calloc(1,sizeof(*b));
    if (!b) return NULL;
    b->count=count;
    b->messages=calloc(count,sizeof(*b->messages));
    b->iov=calloc(count,sizeof(*b->iov));
    b->addresses=calloc(count,sizeof(*b->addresses));
    b->data=calloc(count,STRIDE); b->lengths=calloc(count,sizeof(unsigned));
    if (!b->messages || !b->iov || !b->addresses || !b->data || !b->lengths) {
        udp_batch_free(b); errno=ENOMEM; return NULL;
    }
    for (unsigned i=0;i<count;i++) {
        b->iov[i].iov_base=b->data+i*STRIDE+18; b->iov[i].iov_len=65535;
        b->messages[i].msg_hdr.msg_name=&b->addresses[i];
        b->messages[i].msg_hdr.msg_namelen=sizeof(struct sockaddr_in);
        b->messages[i].msg_hdr.msg_iov=&b->iov[i]; b->messages[i].msg_hdr.msg_iovlen=1;
    }
    return b;
}
void *udp_batch_data(struct batch *b) { return b->data; }
unsigned *udp_batch_lengths(struct batch *b) { return b->lengths; }
int udp_batch_receive(struct batch *b,int fd) {
    int count=recvmmsg(fd,b->messages,b->count,MSG_DONTWAIT,NULL);
    if (count <= 0) return count;
    struct timespec ts;
    if (clock_gettime(CLOCK_REALTIME,&ts)) {
        /* Packets have already left the socket. Return them marked invalid. */
        for (int i=0;i<count;i++) b->lengths[i]=0;
        return count;
    }
    uint64_t stamp=(uint64_t)ts.tv_sec*1000000000ULL+ts.tv_nsec;
    for (int i=0;i<count;i++) {
        struct mmsghdr *m=&b->messages[i];
        if ((m->msg_hdr.msg_flags & MSG_TRUNC) || m->msg_len>65535 ||
            m->msg_hdr.msg_namelen!=sizeof(struct sockaddr_in) || b->addresses[i].sin_family!=AF_INET) {
            b->lengths[i]=0; continue;
        }
        unsigned char *p=b->data+i*STRIDE;
        memcpy(p,&b->addresses[i].sin_addr,4); memcpy(p+4,&b->addresses[i].sin_port,2);
        uint32_t size=htonl(m->msg_len); memcpy(p+6,&size,4);
        for (int n=0;n<8;n++) p[10+n]=(unsigned char)(stamp>>(56-8*n));
        b->lengths[i]=18+m->msg_len;
    }
    return count;
}
