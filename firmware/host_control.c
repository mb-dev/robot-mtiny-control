/* Host-control extension for the mTiny car main MCU, firmware 28.01.005.008 only
 * (published build: 28.01.005.016). Compiled by build_patch.py and linked by hand
 * onto the end of the image produced by the assembled "layer 1" (host activity
 * lease + audio reply bridge, 0x08032e94..0x08033168).
 *
 * Adds: bounded wheel pulses (20 7C), renewable pulses, read-only diagnostics
 * (20 7A), and host-submitted speed-controller steps (20 79).
 * No allocator, no new peripheral initialization, no motor channel 2 writes, no
 * added UART traffic except replies to the new host queries.
 * State lives in newly zero-initialized SRAM above the original stack top.
 * All absolute addresses below are specific to that one firmware build; this is
 * not portable code.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed short s16;
#define B(a) (*(volatile u8 *)(a))
#define W(a) (*(volatile u32 *)(a))
#define CALL0(a) ((void (*)(void))((a)|1u))()
#define GET0(a) ((u32 (*)(void))((a)|1u))()
#define INLINE static __attribute__((always_inline)) inline
struct state { u32 active, started, duration; s16 left, right; u8 reason, sequence, seen, pad; };
#define S ((volatile struct state *)0x20003490)
/* Passive records. ver rows: 0 car radio (7E), 1 pen radio (7F), 2 pen main (3),
 * 3 audio (2). tx/rx count service-0x61 update frames to/from the car radio by
 * subservice (low four bits). Counters saturate; nothing here changes routing.
 */
struct diag { u8 ver[4][4]; u8 count[4]; u32 tick[4]; u16 tx[16]; u16 rx[16]; u8 last[4]; u32 last_tick; };
#define D ((volatile struct diag *)0x200034a4)
#define NOW W(0x200003b0)
/* Host-owned stock speed-controller step. owned is set only by a 20 79 step that
 * passed every gate, and cleared by the tick when the step ends or is cut.
 * reason: 0 running, 1 finished (stock profile left mode 2), 2 time limit,
 * 3 protection, 4 updater, 5 host silent, 6 explicit stop.
 */
struct step { u32 started, limit; u8 owned, reason, sequence, seen; };
#define T ((volatile struct step *)0x20003510)
#define MODE B(0x2000057c)
INLINE u32 lock(void) { u32 p; __asm volatile("mrs %0, primask\ncpsid i" : "=r"(p) :: "memory"); return p; }
INLINE void unlock(u32 p) { __asm volatile("msr primask, %0" :: "r"(p) : "memory"); }
INLINE void motor(u32 c, int v) { ((void (*)(u32,int))0x0800f1a5)(c,v); }
INLINE void stop(u8 reason) {
    S->active=0; S->left=0; S->right=0; S->reason=reason;
    CALL0(0x08016de4); /* both wheels zero, stock motor mode zero */
}
INLINE u8 clip7(u32 v) { return v>127 ? 127 : (u8)v; }

/* Unchanged from .013. */
__attribute__((section(".text.motor_tick"),used)) void motor_tick(void) {
    CALL0(0x0800d91c);
    u32 now=++W(0x200003b0);
    if (S->active) {
        u8 reason=0;
        if ((u32)(now-S->started)>=S->duration) reason=2;
        else if (B(0x200003f8)||B(0x200003f9)) reason=3;
        else if (B(0x200003d4)||B(0x2000057c)) reason=4;
        if (reason) stop(reason);
        else { motor(0,S->left); motor(1,S->right); }
        if (!B(0x200003d4)) CALL0(0x0801a5f8); /* original auxiliary tick */
    } else {
        if (T->owned) {
            u8 reason=0;
            if (MODE!=2) reason=1;
            else if ((u32)(now-T->started)>=T->limit) reason=2;
            else if (B(0x200003f8)||B(0x200003f9)) reason=3;
            else if (B(0x200003d4)) reason=4;
            else if (B(0x20000655)!=2 || !B(0x200008a0) || (u32)(now-W(0x200008a4))>750) reason=5;
            if (reason) {
                T->owned=0; T->reason=reason;
                if (reason>1) CALL0(0x08016de4); /* both wheels zero, stock motor mode zero */
            }
        }
        if (!GET0(0x08026410)) CALL0(0x0800914a);
    }
}

/* IEEE-754 single bits of a small integer (|v| < 2^23); soft-float argument. */
INLINE u32 fbits(int v) {
    u32 sign=0, m;
    if (!v) return 0;
    if (v<0) { sign=0x80000000u; v=-v; }
    m=(u32)v;
    u32 top=31-(u32)__builtin_clz(m);
    return sign | ((127+top)<<23) | ((m<<(23-top))&0x7fffffu);
}

/* Replaces both stock calls to the component-version reporter (0x08028e70).
 * Records the decoded numbers, then runs the stock reporter unchanged.
 */
__attribute__((section(".text.version_report"),used))
void version_report(u32 address, u32 a, u32 b, u32 c, u32 d) {
    u32 row = address==0x7e ? 0 : address==0x7f ? 1 : address==3 ? 2 : address==2 ? 3 : 4;
    if (row<4) {
        D->ver[row][0]=clip7(a); D->ver[row][1]=clip7(b);
        D->ver[row][2]=clip7(c); D->ver[row][3]=clip7(d);
        if (D->count[row]<127) D->count[row]++;
        D->tick[row]=NOW;
    }
    ((void (*)(u32,u32,u32,u32,u32))0x08028e71)(address,a,b,c,d);
}

/* Runs before the .012 audio-reply bridge wrapper (0x08033014), which then
 * continues into the stock router. Reads the frame header only.
 */
__attribute__((section(".text.route_hook"),used)) void route_hook(u8 *descriptor) {
    u32 length=descriptor[0];
    u8 *raw=*(u8 **)(descriptor+4);
    if (length>=6 && raw[1]==0x7e && raw[2]==0x61) {
        u32 bucket=raw[3]&15;
        if (descriptor[8]==1) { if (D->tx[bucket]!=0xffff) D->tx[bucket]++; }
        else if (descriptor[8]==0) {
            if (D->rx[bucket]!=0xffff) D->rx[bucket]++;
            for (u32 i=0;i<4;i++) D->last[i]= (3+i<length-2) ? (raw[3+i]&127) : 0;
            D->last_tick=NOW;
        }
    }
    ((void (*)(u8 *))0x08033015)(descriptor);
}

/* 20 7B seq: snapshot. 20 7C seq left+64 right+64 duration/10ms: pulse.
 * Units are ten original PWM input units, limited to +/-200, <=250 ms.
 * 20 7C seq 40 40 00 is unconditional wheel stop.
 * New in .014: a pulse with a new sequence may replace a running pulse. Every
 * replacement passes all start gates again and restarts the <=250 ms deadline,
 * so motion continues only while fresh, valid pulses keep arriving.
 * Response: 20 selector seq result active reason left:s16 right:s16 mode.
 *
 * 20 7A seq page: read-only diagnostics, response 20 7A seq page result ...
 *   page 0..3: count a b c d age:u32 uptime:u32   (version row, see struct diag)
 *   page 4:    tx[4..9] as six u16                (update frames sent to car radio)
 *   page 5:    rx[4..9] as six u16, last[4], age:u32 (frames from car radio)
 * u32 = stock five 7-bit units, u16 = stock three 7-bit units. Ticks are ms.
 *
 * New in .016:
 * 20 79 seq: step status. 20 79 seq kind a b up down ms_lo ms_hi: run one step
 * through the stock speed controller (0x08009152, the entry the stock sequence
 * player uses). kind 0 forward, 1 backward, 2/3 opposite turns, 4 independent
 * wheels. a and b are (byte-64)*5, limited to +/-200: speed and turn offset for
 * kinds 0..3, left and right wheel speed for kind 4. up/down are ramps in 5 ms
 * units; ms = ms_lo | ms_hi<<7, 20..5000, and up+down must fit inside it.
 * Stock behavior, kept: a step started from idle ignores `up` and ramps over
 * 200 ms (so it lasts at least 200 ms); `up` applies when replacing a running step.
 * A new sequence may replace a running host step (never stock motion or a
 * pulse). The tick cuts a host step when host activity is older than 750 ms.
 * 20 7C seq 40 40 00 stops a step as well as a pulse.
 * Response: 20 79 seq result owned reason mode done phase state left:u32 right:u32
 * (done/phase are the stock profile flags, state the stock activity byte,
 * left/right the signed wheel encoder positions).
 */
__attribute__((section(".text.motor_query"),used)) void motor_query(u8 *request) {
    u32 length=request[9];
    u8 *body=*(u8 **)(request+4);
    if (length<2 || (body[1]<0x79 || body[1]>0x7c)) {
        ((void (*)(u8 *))0x08032e95)(request); return;
    }
    if (body[0]!=0x20 || length<3 || body[2]>127) return;
    u8 selector=body[1], sequence=body[2], result=0;
    u8 cursor[8], buffer[48];
    ((void (*)(void *,void *,u32,u32))0x08009799)(cursor,buffer,48,0);
#define PUT(v) ((void (*)(void *,u32))0x08009a25)(cursor,(v))
#define PUT16(v) ((void (*)(void *,u32))0x08009bd5)(cursor,(u16)(v))
#define PUT32(v) ((void (*)(void *,u32))0x08009ae1)(cursor,(v))
    if (selector==0x7a) {
        u32 page = length==4 ? body[3] : 255, now=NOW;
        PUT(0x20); PUT(selector); PUT(sequence); PUT(page&127);
        if (page>5) PUT(1);
        else if (page<4) {
            PUT(0); PUT(D->count[page]);
            for (u32 i=0;i<4;i++) PUT(D->ver[page][i]);
            PUT32(now-D->tick[page]); PUT32(now);
        } else {
            volatile u16 *table = page==4 ? D->tx : D->rx;
            PUT(0);
            for (u32 i=4;i<10;i++) PUT16(table[i]);
            if (page==5) { for (u32 i=0;i<4;i++) PUT(D->last[i]); PUT32(now-D->last_tick); }
        }
        ((void (*)(void *,void *))0x0801f1db)(cursor,request);
        return;
    }
    u32 irq=lock();
    if (selector==0x79) {
        if (length!=3) {
            u32 ms = length==10 ? (body[8]|((u32)body[9]<<7)) : 0;
            u32 up=body[6]*5u, down=body[7]*5u;
            if (length!=10 || body[3]>4 || body[4]<24 || body[4]>104 || body[5]<24 || body[5]>104
                || body[6]>127 || body[7]>127 || body[8]>127 || body[9]>127
                || ms<20 || ms>5000 || up+down>ms) result=1;
            else if (S->active || (MODE && !(MODE==2 && T->owned))) result=2;
            else if (B(0x200003f8)||B(0x200003f9)||B(0x200003d4)||B(0x200003e4)!=2) result=3;
            else if (request[10]!=0 || B(0x20000655)!=2 || !B(0x200008a0) || (u32)(NOW-W(0x200008a4))>750) result=4;
            else if (T->seen && T->sequence==sequence) result=5;
            else {
                T->started=NOW; T->limit=ms+300; T->reason=0; T->sequence=sequence; T->seen=1;
                ((void (*)(u32,u32,u32,u32,u32,u32,u32))0x08009153)(
                    body[3], fbits(((int)body[4]-64)*5), fbits(((int)body[5]-64)*5), up, down, ms, 0);
                T->owned=1;
            }
        }
        u8 owned=T->owned, why=T->reason, mode=MODE, done=B(0x20000541), phase=B(0x20000542), state=B(0x20000416);
        u32 left=((u32 (*)(u32))0x0800ef01)(0), right=((u32 (*)(u32))0x0800ef01)(1);
        unlock(irq);
        PUT(0x20); PUT(selector); PUT(sequence); PUT(result); PUT(owned); PUT(why); PUT(mode);
        PUT(done&127); PUT(phase&127); PUT(state&127); PUT32(left); PUT32(right);
        ((void (*)(void *,void *))0x0801f1db)(cursor,request);
        return;
    }
    if (selector==0x7b) { if (length!=3) result=1; }
    else if (length!=6 || body[3]<44 || body[3]>84 || body[4]<44 || body[4]>84 || body[5]>25) result=1;
    else if (body[3]==64 && body[4]==64 && body[5]==0) {
        if (T->owned) { T->owned=0; T->reason=6; }
        stop(1);
    }
    else if (!body[5]) result=1;
    else if (B(0x2000057c)) result=2;
    else if (B(0x200003f8)||B(0x200003f9)||B(0x200003d4)||B(0x200003e4)!=2) result=3;
    else if (request[10]!=0 || B(0x20000655)!=2 || !B(0x200008a0) || (u32)(W(0x200003b0)-W(0x200008a4))>750) result=4;
    else if (S->seen && S->sequence==sequence) result=5;
    else {
        S->left=((int)body[3]-64)*10; S->right=((int)body[4]-64)*10;
        S->duration=body[5]*10; S->started=W(0x200003b0);
        S->reason=0; S->sequence=sequence; S->seen=1; S->active=1;
        motor(0,S->left); motor(1,S->right);
    }
    u8 active=S->active, reason=S->reason, mode=B(0x2000057c);
    s16 left=*(volatile s16 *)0x20000a30, right=*(volatile s16 *)0x20000a32;
    unlock(irq);
    PUT(0x20); PUT(selector); PUT(sequence); PUT(result); PUT(active); PUT(reason);
    PUT16(left); PUT16(right);
    PUT(mode);
    ((void (*)(void *,void *))0x0801f1db)(cursor,request);
}
