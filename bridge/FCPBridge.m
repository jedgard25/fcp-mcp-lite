// FCPBridge.m — minimal agent bridge for Final Cut Pro.
//
// One file, 7 verbs + one in-process Transcript panel. Patterns adapted from
// SpliceKit (MIT):
//   - NSApp.delegate -> activeEditorContainer -> timelineModule
//   - sequence -> primaryObject -> containedItems (spine walk)
//   - IBAction-style selectors on FFAnchoredTimelineModule with sender=nil
//   - NSInvocation for struct (CMTime) returns — ABI-safe on arm64 + x86_64
//   - TCP JSON-RPC 2.0 on 127.0.0.1:9876, newline-delimited
//   - Window > Transcript (Cmd+0): in-process WKWebView panel on
//     http://127.0.0.1:8765/ — no Workflow Extension SDK, no .appex, no
//     /Applications install. Shows MCP status until transcribed (ui_server).
//
// Deliberately NOT included: runtime introspection, plugin loader,
// transcription, captions, debug toolkit. Brains live in mcp/server.py;
// this file is dumb pipes with defensive respondsToSelector: checks.

#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <CoreMedia/CoreMedia.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <sys/socket.h>
#import <netinet/in.h>
#import <unistd.h>

#define FCB_PORT 9876
#define FCB_VERSION @"0.3.0"

// Matches CMTime layout {value:int64, timescale:int32, flags:uint32, epoch:int64}.
typedef struct { int64_t value; int32_t timescale; uint32_t flags; int64_t epoch; } FCB_CMTime;
typedef struct { FCB_CMTime start; FCB_CMTime duration; } FCB_CMTimeRange;

#define FCB_LOG(fmt, ...) NSLog(@"[FCPBridge] " fmt, ##__VA_ARGS__)

// ---------------------------------------------------------------- helpers

static void FCB_runOnMain(dispatch_block_t block) {
    if ([NSThread isMainThread]) { block(); return; }
    dispatch_sync(dispatch_get_main_queue(), block);
}

static id FCB_activeTimeline(void) {
    id app = ((id (*)(id, SEL))objc_msgSend)(
        (id)objc_getClass("NSApplication"), @selector(sharedApplication));
    id delegate = ((id (*)(id, SEL))objc_msgSend)(app, @selector(delegate));
    if (!delegate) return nil;
    SEL aecSel = @selector(activeEditorContainer);
    if (![delegate respondsToSelector:aecSel]) return nil;
    id container = ((id (*)(id, SEL))objc_msgSend)(delegate, aecSel);
    if (!container) return nil;
    SEL tmSel = NSSelectorFromString(@"timelineModule");
    if ([container respondsToSelector:tmSel])
        return ((id (*)(id, SEL))objc_msgSend)(container, tmSel);
    return nil;
}

// Struct-returning call via NSInvocation (correct on every arch).
static BOOL FCB_structCall(id target, SEL sel, id arg, void *retValue) {
    if (!target || ![target respondsToSelector:sel]) return NO;
    NSMethodSignature *sig = [target methodSignatureForSelector:sel];
    if (!sig) return NO;
    NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
    [inv setTarget:target];
    [inv setSelector:sel];
    if (arg) [inv setArgument:&arg atIndex:2];
    @try { [inv invoke]; } @catch (NSException *e) { return NO; }
    if (retValue && strcmp([sig methodReturnType], "v") != 0)
        [inv getReturnValue:retValue];
    return YES;
}

static double FCB_seconds(FCB_CMTime t) {
    if (t.timescale <= 0) return 0;
    return (double)t.value / (double)t.timescale;
}

static double FCB_timeOf(id target, SEL sel) {
    FCB_CMTime t = {0, 0, 0, 0};
    if (!FCB_structCall(target, sel, nil, &t)) return -1;
    return FCB_seconds(t);
}

static NSString *FCB_itemName(id item) {
    for (NSString *selName in @[@"displayName", @"name"]) {
        SEL s = NSSelectorFromString(selName);
        if ([item respondsToSelector:s]) {
            @try {
                id v = ((id (*)(id, SEL))objc_msgSend)(item, s);
                if ([v isKindOfClass:[NSString class]] && [(NSString *)v length] > 0)
                    return v;
            } @catch (NSException *e) {}
        }
    }
    return NSStringFromClass([item class]);
}

// media.originalMediaURL -> clipRef.assets[].originalMediaURL ->
// assetMediaReference.resolvedURL. Every hop guarded.
static NSString *FCB_mediaPathForClip(id clip) {
    @try {
        SEL mediaSel = NSSelectorFromString(@"media");
        if ([clip respondsToSelector:mediaSel]) {
            id media = ((id (*)(id, SEL))objc_msgSend)(clip, mediaSel);
            if (media) {
                SEL omSel = NSSelectorFromString(@"originalMediaURL");
                if ([media respondsToSelector:omSel]) {
                    id url = ((id (*)(id, SEL))objc_msgSend)(media, omSel);
                    if ([url isKindOfClass:[NSURL class]] && [(NSURL *)url isFileURL])
                        return [(NSURL *)url path];
                }
            }
        }
        SEL clipRefSel = NSSelectorFromString(@"clipRef");
        if ([clip respondsToSelector:clipRefSel]) {
            id clipRef = ((id (*)(id, SEL))objc_msgSend)(clip, clipRefSel);
            SEL assetsSel = NSSelectorFromString(@"assets");
            if (clipRef && [clipRef respondsToSelector:assetsSel]) {
                id assets = ((id (*)(id, SEL))objc_msgSend)(clipRef, assetsSel);
                NSArray *arr = nil;
                if ([assets isKindOfClass:[NSSet class]]) arr = [(NSSet *)assets allObjects];
                else if ([assets isKindOfClass:[NSArray class]]) arr = assets;
                SEL omSel = NSSelectorFromString(@"originalMediaURL");
                for (id a in arr) {
                    if ([a respondsToSelector:omSel]) {
                        id url = ((id (*)(id, SEL))objc_msgSend)(a, omSel);
                        if ([url isKindOfClass:[NSURL class]] && [(NSURL *)url isFileURL])
                            return [(NSURL *)url path];
                    }
                }
            }
        }
        SEL amrSel = NSSelectorFromString(@"assetMediaReference");
        if ([clip respondsToSelector:amrSel]) {
            id ref = ((id (*)(id, SEL))objc_msgSend)(clip, amrSel);
            SEL ruSel = NSSelectorFromString(@"resolvedURL");
            if (ref && [ref respondsToSelector:ruSel]) {
                id url = ((id (*)(id, SEL))objc_msgSend)(ref, ruSel);
                if ([url isKindOfClass:[NSURL class]] && [(NSURL *)url isFileURL])
                    return [(NSURL *)url path];
            }
        }
    } @catch (NSException *e) {}
    return nil;
}

// ---------------------------------------------------------------- verbs

// Stable clip identity for the session. The store retains objects (never a
// dangling pointer); membership is re-verified against the live timeline on
// every use, so a deleted clip reads as STALE, never as wrong.
static NSMutableDictionary<NSString *, id> *sHandleObjs = nil;
static NSMutableDictionary<NSValue *, NSString *> *sHandlePtrs = nil;
static NSInteger sHandleCounter = 0;

static NSString *FCB_idForObject(id obj) {
    if (!obj) return nil;
    if (!sHandleObjs) {
        sHandleObjs = [NSMutableDictionary dictionary];
        sHandlePtrs = [NSMutableDictionary dictionary];
    }
    NSValue *key = [NSValue valueWithPointer:(const void *)obj];
    NSString *existing = sHandlePtrs[key];
    if (existing) return existing;
    NSString *nid = [NSString stringWithFormat:@"clip_%ld", (long)++sHandleCounter];
    sHandleObjs[nid] = obj;
    sHandlePtrs[key] = nid;
    return nid;
}

// Every timeline object reachable from the sequence (spine + one nested
// level), as pointer values for membership tests.
static NSMutableArray<NSValue *> *FCB_livePointers(id sequence) {
    NSMutableArray<NSValue *> *ptrs = [NSMutableArray array];
    id primaryObj = nil;
    if ([sequence respondsToSelector:@selector(primaryObject)])
        primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
    if (!primaryObj || ![primaryObj respondsToSelector:@selector(containedItems)]) return ptrs;
    @try {
        id raw = ((id (*)(id, SEL))objc_msgSend)(primaryObj, @selector(containedItems));
        if (![raw isKindOfClass:[NSArray class]]) return ptrs;
        for (id item in (NSArray *)raw) {
            [ptrs addObject:[NSValue valueWithPointer:(const void *)item]];
            NSString *cls = NSStringFromClass([item class]) ?: @"";
            BOOL isMedia = [cls containsString:@"MediaComponent"] || [cls containsString:@"AnchoredClip"];
            BOOL isGap = [cls containsString:@"Gap"];
            if (isMedia && !isGap) {
                // Nested items anchored to a media clip (see FCB_appendClip).
                @try {
                    SEL aiSel = NSSelectorFromString(@"anchoredItems");
                    if ([item respondsToSelector:aiSel]) {
                        id nraw = ((id (*)(id, SEL))objc_msgSend)(item, aiSel);
                        NSArray *subs = nil;
                        if ([nraw isKindOfClass:[NSSet class]]) subs = [(NSSet *)nraw allObjects];
                        else if ([nraw isKindOfClass:[NSArray class]]) subs = nraw;
                        for (id sub in subs ?: @[])
                            [ptrs addObject:[NSValue valueWithPointer:(const void *)sub]];
                    }
                } @catch (NSException *e) {}
                continue;
            }
            if (!isMedia && !isGap) {
                id inner = nil;
                if ([item respondsToSelector:@selector(containedItems)]) {
                    @try { inner = ((id (*)(id, SEL))objc_msgSend)(item, @selector(containedItems)); }
                    @catch (NSException *e) {}
                }
                if ([inner isKindOfClass:[NSArray class]])
                    for (id sub in (NSArray *)inner)
                        [ptrs addObject:[NSValue valueWithPointer:(const void *)sub]];
            }
        }
    } @catch (NSException *e) {}
    return ptrs;
}

static NSDictionary *FCB_systemVersion(void) {
    NSString *fcp = [[[NSBundle mainBundle] infoDictionary]
        objectForKey:@"CFBundleShortVersionString"];
    return @{@"fcp": fcp ?: @"?", @"bridge": FCB_VERSION};
}

// Source-space range candidates for the trim (file offset) probe. Deliberately
// source-only: timeline-space ranges (effectiveRangeOfObject:/anchoredOffset)
// would alias timeline_start as trim and must never enter this list.
static NSArray<NSString *> *FCB_trimRangeSelectors(void) {
    return @[@"unclippedRange", @"sourceRange", @"mediaRange", @"trimmedRange",
             @"sourceTimeRange", @"untrimmedRange", @"originalRange", @"availableRange"];
}

// Probe every trim candidate on a clip. Returns start->seconds for each sane
// hit (duration matches the clip within half a second — filters out full-media
// ranges). The caller picks the consensus; disagreement means "unknown".
static NSMutableDictionary *FCB_trimCandidatesForClip(id item, double clipDur) {
    NSMutableDictionary *out = [NSMutableDictionary dictionary];
    for (NSString *name in FCB_trimRangeSelectors()) {
        SEL s = NSSelectorFromString(name);
        if (![item respondsToSelector:s]) continue;
        FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
        if (!FCB_structCall(item, s, nil, &u)) continue;
        double d = FCB_seconds(u.duration);
        if (d <= 0) continue;
        if (clipDur > 0) {
            double dd = d - clipDur;
            if (dd < 0) dd = -dd;
            if (dd > 0.5) continue;
        }
        double st = FCB_seconds(u.start);
        if (st < 0) continue;
        out[name] = @(st);
    }
    return out;
}

static double FCB_trimStartForClip(id item, double clipDur, double clipStart, NSDictionary **candsOut) {
    NSMutableDictionary *cands = FCB_trimCandidatesForClip(item, clipDur);
    // The per-clip source in-point lives one hop away: a member of
    // anchoredTimelineItems carries sourceAnchorTime/localAnchorTime
    // (verified: 0 for the first clip, 8.51 for clip_2, 1809.14 for a late
    // clip — each with trim+dur inside the full media length).
    // detachAudio exposes the same values but its name promises mutation —
    // never call it in production. The hop array's order varies, so scan
    // members for the geometric match (timelineRange == this clip) and
    // accept only if trim + clipDur fits inside the full media length.
    @try {
        SEL aiSel = NSSelectorFromString(@"anchoredTimelineItems");
        if ([item respondsToSelector:aiSel]) {
            id arr = ((id (*)(id, SEL))objc_msgSend)(item, aiSel);
            NSArray *list = nil;
            if ([arr isKindOfClass:[NSArray class]]) list = arr;
            else if ([arr isKindOfClass:[NSSet class]]) list = [(NSSet *)arr allObjects];
            double fullDur = 0;
            FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
            if (FCB_structCall(item, NSSelectorFromString(@"unclippedRange"), nil, &u))
                fullDur = FCB_seconds(u.duration);
            for (id hop in list ?: @[]) {
                FCB_CMTimeRange tr = {{0,0,0,0},{0,0,0,0}};
                if (!FCB_structCall(hop, NSSelectorFromString(@"timelineRange"), nil, &tr))
                    continue;
                double dd = FCB_seconds(tr.duration) - clipDur;
                if (dd < 0) dd = -dd;
                if (dd > 0.5) continue;
                double ds = FCB_seconds(tr.start) - clipStart;
                if (ds < 0) ds = -ds;
                if (ds > 0.05) continue;
                for (NSString *nm in @[@"sourceAnchorTime", @"localAnchorTime"]) {
                    SEL ts = NSSelectorFromString(nm);
                    if (![hop respondsToSelector:ts]) continue;
                    FCB_CMTime t = {0,0,0,0};
                    if (!FCB_structCall(hop, ts, nil, &t) || t.timescale <= 0) continue;
                    double v = FCB_seconds(t);
                    if (v < 0) continue;
                    if (fullDur > 0 && v + clipDur > fullDur + 0.5) continue;
                    cands[@"anchoredTimelineItems.sourceAnchorTime"] = @(v);
                    break;
                }
                if (cands[@"anchoredTimelineItems.sourceAnchorTime"]) break;
            }
        }
    } @catch (NSException *e) {}
    // Primary trim path: parentToLocalOffset maps parent (spine) time to
    // local (source) time, i.e. trim = timeline_start + offset (verified:
    // 0 for the first clip, 2.0 for clip_2, 242.31 for a late clip —
    // each consistent with the logged cut history). Same full-length
    // bound as above; rejected values leave the clip degenerate (blocked,
    // never wrong-cut). Python re-validates the whole tiling.
    @try {
        SEL poSel = NSSelectorFromString(@"parentToLocalOffset");
        if ([item respondsToSelector:poSel]) {
            FCB_CMTime off = {0,0,0,0};
            if (FCB_structCall(item, poSel, nil, &off) && off.timescale > 0) {
                double v = clipStart + FCB_seconds(off);
                if (v >= 0) {
                    double fullDur = 0;
                    FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
                    if (FCB_structCall(item, NSSelectorFromString(@"unclippedRange"), nil, &u))
                        fullDur = FCB_seconds(u.duration);
                    if (fullDur <= 0 || v + clipDur <= fullDur + 0.5)
                        cands[@"parentToLocalOffset-derived"] = @(v);
                }
            }
        }
    } @catch (NSException *e) {}
    if (candsOut) *candsOut = cands;
    double best = 0;
    for (NSNumber *n in [cands allValues])
        if ([n doubleValue] > best) best = [n doubleValue];
    return best;
}

static void FCB_appendClip(NSMutableArray *out, id item, id primaryObj,
                           NSSet *selected, NSInteger *idx, NSString *lane,
                           int depth, double *durAcc) {
    NSString *cls = NSStringFromClass([item class]) ?: @"";
    BOOL isMedia = [cls containsString:@"MediaComponent"] || [cls containsString:@"AnchoredClip"];
    BOOL isGap = [cls containsString:@"Gap"];
    if (!isMedia && !isGap) {
        // Container (compound / storyline / multicam): emit it addressably so
        // step-in can target it, then recurse one level for its members.
        double cdur = FCB_timeOf(item, @selector(duration));
        if (cdur > 0) {
            double cstart = -1;
            if (primaryObj) {
                FCB_CMTimeRange cr = {{0,0,0,0},{0,0,0,0}};
                if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), item, &cr))
                    cstart = FCB_seconds(cr.start);
            }
            if (cstart < 0) cstart = 0;
            NSMutableDictionary *cd = [NSMutableDictionary dictionary];
            cd[@"index"] = @((*idx)++);
            NSString *ccid = FCB_idForObject(item);
            if (ccid) cd[@"id"] = ccid;
            cd[@"class"] = cls;
            cd[@"name"] = FCB_itemName(item);
            cd[@"lane"] = lane;
            cd[@"container"] = @YES;
            cd[@"timeline_start_s"] = @(cstart);
            cd[@"duration_s"] = @(cdur);
            if (selected) cd[@"selected"] = @([selected containsObject:item]);
            [out addObject:cd];
        }
        id inner = nil;
        if ([item respondsToSelector:@selector(containedItems)]) {
            @try { inner = ((id (*)(id, SEL))objc_msgSend)(item, @selector(containedItems)); }
            @catch (NSException *e) {}
        }
        if (![inner isKindOfClass:[NSArray class]] && [item respondsToSelector:@selector(anchoredItems)]) {
            @try {
                id raw = ((id (*)(id, SEL))objc_msgSend)(item, NSSelectorFromString(@"anchoredItems"));
                if ([raw isKindOfClass:[NSSet class]]) inner = [(NSSet *)raw allObjects];
                else if ([raw isKindOfClass:[NSArray class]]) inner = raw;
            } @catch (NSException *e) {}
        }
        if ([inner isKindOfClass:[NSArray class]]) {
            for (id sub in (NSArray *)inner)
                FCB_appendClip(out, sub, primaryObj, selected, idx, @"connected", depth + 1, durAcc);
        }
        return;
    }
    double dur = FCB_timeOf(item, @selector(duration));
    if (dur <= 0) return;
    if (depth == 0 && !isGap && durAcc) *durAcc += dur;
    // Timeline start: effectiveRangeOfObject: on the spine, else anchoredOffset.
    double start = -1;
    if (primaryObj) {
        FCB_CMTimeRange r = {{0,0,0,0},{0,0,0,0}};
        if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), item, &r))
            start = FCB_seconds(r.start);
    }
    if (start < 0)
        start = FCB_timeOf(item, NSSelectorFromString(@"anchoredOffset"));
    if (start < 0) start = 0;
    double trim = 0;
    NSDictionary *cands = nil;
    @try { trim = FCB_trimStartForClip(item, dur, start, &cands); }
    @catch (NSException *e) { trim = 0; cands = nil; }
    NSMutableDictionary *d = [NSMutableDictionary dictionary];
    d[@"index"] = @((*idx)++);
    NSString *cid = FCB_idForObject(item);
    if (cid) d[@"id"] = cid;
    d[@"class"] = cls;
    d[@"name"] = FCB_itemName(item);
    d[@"lane"] = lane;
    if ([cls containsString:@"Collection"] || [cls containsString:@"Compound"]
        || [cls containsString:@"Storyline"] || [cls containsString:@"Multicam"])
        d[@"container"] = @YES;
    d[@"timeline_start_s"] = @(start);
    d[@"duration_s"] = @(dur);
    d[@"trim_start_s"] = @(trim);
    if (cands && [cands count] > 0) d[@"trim_candidates"] = cands;
    if (selected) d[@"selected"] = @([selected containsObject:item]);
    NSString *path = isMedia ? FCB_mediaPathForClip(item) : nil;
    if (path) d[@"media_path"] = path;
    [out addObject:d];
    // Nested items anchored directly to a media clip (connected audio,
    // titles, markers-as-items). One level only. These are addressable
    // (registered IDs) so a stray can be selected + deleted like any clip.
    @try {
        SEL aiSel = NSSelectorFromString(@"anchoredItems");
        if ([item respondsToSelector:aiSel]) {
            id raw = ((id (*)(id, SEL))objc_msgSend)(item, aiSel);
            NSArray *subs = nil;
            if ([raw isKindOfClass:[NSSet class]]) subs = [(NSSet *)raw allObjects];
            else if ([raw isKindOfClass:[NSArray class]]) subs = raw;
            for (id sub in subs ?: @[]) {
                if (sub == item) continue;
                double sdur = FCB_timeOf(sub, @selector(duration));
                if (sdur <= 0) continue;
                double sstart = start;
                if (primaryObj) {
                    FCB_CMTimeRange sr = {{0,0,0,0},{0,0,0,0}};
                    if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), sub, &sr))
                        sstart = FCB_seconds(sr.start);
                }
                NSMutableDictionary *nd = [NSMutableDictionary dictionary];
                nd[@"index"] = @((*idx)++);
                NSString *nid = FCB_idForObject(sub);
                if (nid) nd[@"id"] = nid;
                nd[@"class"] = NSStringFromClass([sub class]) ?: @"?";
                nd[@"name"] = FCB_itemName(sub);
                nd[@"lane"] = @"nested";
                if (cid) nd[@"parent"] = cid;
                nd[@"timeline_start_s"] = @(sstart);
                nd[@"duration_s"] = @(sdur);
                if (selected) nd[@"selected"] = @([selected containsObject:sub]);
                [out addObject:nd];
            }
        }
    } @catch (NSException *e) {}
}

static NSDictionary *FCB_timelineClips(void) {
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline. Open a project first."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        NSMutableDictionary *state = [NSMutableDictionary dictionary];
        @try { state[@"sequence_name"] = FCB_itemName(sequence); } @catch (NSException *e) {}
        double fps = FCB_timeOf(timeline, NSSelectorFromString(@"sequenceFrameDuration"));
        // sequenceFrameDuration is a duration; fps = timescale/value.
        if (fps > 0) {
            FCB_CMTime fd = {0,0,0,0};
            if (FCB_structCall(timeline, NSSelectorFromString(@"sequenceFrameDuration"), nil, &fd)
                && fd.value > 0)
                state[@"fps"] = @((double)fd.timescale / (double)fd.value);
        }
        double ph = FCB_timeOf(timeline, NSSelectorFromString(@"playheadTime"));
        if (ph >= 0) state[@"playhead_s"] = @(ph);
        double dur = FCB_timeOf(sequence, @selector(duration));
        NSSet *selected = nil;
        SEL selSel = NSSelectorFromString(@"selectedItems:includeItemBeforePlayheadIfLast:");
        if ([timeline respondsToSelector:selSel]) {
            @try {
                id s = ((id (*)(id, SEL, BOOL, BOOL))objc_msgSend)(timeline, selSel, NO, NO);
                if ([s isKindOfClass:[NSArray class]]) selected = [NSSet setWithArray:s];
            } @catch (NSException *e) {}
        }
        id primaryObj = nil;
        NSArray *items = nil;
        if ([sequence respondsToSelector:@selector(primaryObject)]) {
            primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
            if ([primaryObj respondsToSelector:@selector(containedItems)]) {
                @try {
                    id raw = ((id (*)(id, SEL))objc_msgSend)(primaryObj, @selector(containedItems));
                    if ([raw isKindOfClass:[NSArray class]]) items = raw;
                } @catch (NSException *e) {}
            }
        }
        NSMutableArray *out = [NSMutableArray array];
        NSInteger idx = 0;
        double spineDur = 0;
        for (id item in items ?: @[])
            FCB_appendClip(out, item, primaryObj, selected, &idx, @"primary", 0, &spineDur);
        if (dur > 0) state[@"duration_s"] = @(dur);
        else if (spineDur > 0) state[@"duration_s"] = @(spineDur); // fallback: sum spine
        else {
            // fallback: composition extent (covers nested/compound timelines)
            double end = 0;
            for (NSDictionary *c in out) {
                double e = [c[@"timeline_start_s"] doubleValue] + [c[@"duration_s"] doubleValue];
                if (e > end) end = e;
            }
            if (end > 0) state[@"duration_s"] = @(end);
        }
        state[@"clips"] = out;
        result = state;
    });
    return result;
}

static NSDictionary *FCB_playbackPosition(void) {
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        NSMutableDictionary *d = [NSMutableDictionary dictionary];
        double ph = FCB_timeOf(timeline, NSSelectorFromString(@"playheadTime"));
        if (ph >= 0) d[@"t_s"] = @(ph);
        FCB_CMTime fd = {0,0,0,0};
        if (FCB_structCall(timeline, NSSelectorFromString(@"sequenceFrameDuration"), nil, &fd)
            && fd.value > 0)
            d[@"fps"] = @((double)fd.timescale / (double)fd.value);
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (sequence) {
            double dur = FCB_timeOf(sequence, @selector(duration));
            if (dur > 0) d[@"duration_s"] = @(dur);
        }
        result = d;
    });
    return result;
}

static NSDictionary *FCB_playbackSeek(NSDictionary *params) {
    double t = [params[@"t_s"] doubleValue];
    if (t < 0) return @{@"error": @"t_s must be >= 0"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        SEL s = NSSelectorFromString(@"setPlayheadTime:");
        if (![timeline respondsToSelector:s]) {
            result = @{@"error": @"Timeline does not respond to setPlayheadTime:"};
            return;
        }
        CMTime ct = CMTimeMakeWithSeconds(t, 600000);
        NSMethodSignature *sig = [timeline methodSignatureForSelector:s];
        if (!sig) { result = @{@"error": @"No signature for setPlayheadTime:"}; return; }
        NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
        [inv setTarget:timeline];
        [inv setSelector:s];
        [inv setArgument:&ct atIndex:2];
        @try { [inv invoke]; } @catch (NSException *e) {
            result = @{@"error": [NSString stringWithFormat:@"seek failed: %@", e.reason]};
            return;
        }
        result = @{@"t_s": @(t), @"status": @"ok"};
    });
    return result;
}

static NSDictionary *FCB_timelineAction(NSDictionary *params) {
    static NSDictionary *allow = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        allow = @{
            @"blade": @"blade:", @"bladeAll": @"bladeAll:",
            @"delete": @"delete:", @"cut": @"cut:", @"copy": @"copy:", @"paste": @"paste:",
            @"openCompound": @"openInTimeline:",
            @"timelineBack": @"timelineHistoryBack:",
            @"selectClipAtPlayhead": @"selectClipAtPlayhead:",
            @"selectAll": @"selectAll:", @"deselectAll": @"deselectAll:",
            @"addMarker": @"addMarker:", @"addChapterMarker": @"addChapterMarker:",
            @"nextEdit": @"nextEdit:", @"previousEdit": @"previousEdit:",
            @"trimToPlayhead": @"trimToPlayhead:",
        };
    });
    NSString *action = params[@"action"];
    NSString *selName = action ? allow[action] : nil;
    if (!selName)
        return @{@"error": [NSString stringWithFormat:@"unknown or disallowed action: %@",
                            action ?: @"(null)"]};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        SEL sel = NSSelectorFromString(selName);
        if (![timeline respondsToSelector:sel]) {
            result = @{@"error": [NSString stringWithFormat:@"Timeline does not respond to %@", selName]};
            return;
        }
        @try {
            ((void (*)(id, SEL, id))objc_msgSend)(timeline, sel, nil);
            result = @{@"action": action, @"status": @"ok"};
        } @catch (NSException *e) {
            result = @{@"error": [NSString stringWithFormat:@"Exception: %@", e.reason]};
        }
    });
    return result;
}

static NSDictionary *FCB_timelineSelect(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        // Membership: the retained object must still sit in the live timeline.
        NSMutableArray<NSValue *> *live = FCB_livePointers(sequence);
        NSValue *tkey = [NSValue valueWithPointer:(const void *)target];
        if (![live containsObject:tkey]) {
            result = @{@"error": [NSString stringWithFormat:@"stale id %@ (%@) — clip left the timeline, re-read timeline.clips", cid, FCB_itemName(target)]};
            return;
        }
        // Position the playhead inside the clip, select, then confirm the
        // selection IS our object — never trust a blind select on a stack.
        double start = -1, dur = 0;
        id primaryObj = nil;
        if ([sequence respondsToSelector:@selector(primaryObject)])
            primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
        if (primaryObj) {
            FCB_CMTimeRange r = {{0,0,0,0},{0,0,0,0}};
            if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), target, &r)) {
                start = FCB_seconds(r.start);
                dur = FCB_seconds(r.duration);
            }
        }
        if (start < 0 || dur <= 0) {
            result = @{@"error": [NSString stringWithFormat:@"cannot resolve position of %@ — refusing blind select", cid]};
            return;
        }
        NSDictionary *seekR = FCB_playbackSeek(@{@"t_s": @(start + dur / 2)});
        if (seekR[@"error"]) { result = seekR; return; }
        SEL selSel = NSSelectorFromString(@"selectClipAtPlayhead:");
        if (![timeline respondsToSelector:selSel]) {
            result = @{@"error": @"Timeline does not respond to selectClipAtPlayhead:"};
            return;
        }
        @try { ((void (*)(id, SEL, id))objc_msgSend)(timeline, selSel, nil); }
        @catch (NSException *e) {
            result = @{@"error": [NSString stringWithFormat:@"select failed: %@", e.reason]};
            return;
        }
        SEL selItemsSel = NSSelectorFromString(@"selectedItems:includeItemBeforePlayheadIfLast:");
        BOOL confirmed = NO;
        NSString *gotName = nil;
        if ([timeline respondsToSelector:selItemsSel]) {
            @try {
                id selItems = ((id (*)(id, SEL, BOOL, BOOL))objc_msgSend)(timeline, selItemsSel, NO, NO);
                if ([selItems isKindOfClass:[NSArray class]]) {
                    for (id s in (NSArray *)selItems) {
                        if (s == target) { confirmed = YES; break; }
                        gotName = FCB_itemName(s);
                    }
                }
            } @catch (NSException *e) {}
        }
        if (!confirmed) {
            SEL dsel = NSSelectorFromString(@"deselectAll:");
            if ([timeline respondsToSelector:dsel]) {
                @try { ((void (*)(id, SEL, id))objc_msgSend)(timeline, dsel, nil); }
                @catch (NSException *e) {}
            }
            result = @{@"error": [NSString stringWithFormat:
                @"selection landed on %@ instead of %@ — deselected, nothing acted on",
                gotName ?: @"nothing", FCB_itemName(target)]};
            return;
        }
        result = @{@"id": cid, @"name": FCB_itemName(target), @"status": @"ok"};
    });
    return result;
}

// Read a no-arg double/float selector safely (encoding-checked; anything
// else is skipped, never misread).
static id FCB_probeDouble(id obj, NSString *name) {
    SEL s = NSSelectorFromString(name);
    if (![obj respondsToSelector:s]) return nil;
    NSMethodSignature *sig = [obj methodSignatureForSelector:s];
    if (!sig || [sig numberOfArguments] != 2) return nil;
    const char *rt = [sig methodReturnType];
    if (rt[0] != 'd' && rt[0] != 'f') return nil;
    NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
    [inv setTarget:obj];
    [inv setSelector:s];
    @try { [inv invoke]; } @catch (NSException *e) { return nil; }
    if (rt[0] == 'd') { double v = 0; [inv getReturnValue:&v]; return @(v); }
    float v = 0; [inv getReturnValue:&v]; return @((double)v);
}

static NSDictionary *FCB_probeObject(id obj, NSString *label) {
    NSArray *rangeSels = @[@"unclippedRange", @"sourceRange", @"mediaRange",
        @"trimmedRange", @"sourceTimeRange", @"untrimmedRange", @"originalRange",
        @"availableRange", @"referenceRange", @"anchoredRange", @"storyRange",
        @"parentRange", @"clipMediaRange", @"sourceMediaRange", @"contentMediaRange",
        @"timelineRange"];
    NSArray *timeSels = @[@"sourceStartTime", @"mediaStartTime", @"trimStartTime",
        @"startOffsetTime", @"sourceTime", @"mediaTime", @"unclippedStartTime",
        @"inPoint", @"outPoint", @"sourceInPoint", @"sourceOutPoint",
        @"mediaStart", @"sourceStart", @"startTime", @"anchorTime", @"anchoredOffset",
        @"sourceAnchorTime", @"targetAnchorTime", @"localAnchorTime",
        @"timelineAnchorOffset", @"timelineParentAnchorOffset",
        @"localToParentOffset", @"parentToLocalOffset", @"timeOffset",
        @"offsetExportValue"];
    NSArray *dblSels = @[@"trimStart", @"trimEnd", @"trimDuration", @"sourceStart",
        @"mediaStart", @"startOffset", @"sourceOffset", @"anchorOffset",
        @"inPointValue", @"outPointValue", @"sourceIn", @"sourceOut"];
    NSMutableDictionary *ranges = [NSMutableDictionary dictionary];
    NSMutableDictionary *times = [NSMutableDictionary dictionary];
    NSMutableDictionary *dbls = [NSMutableDictionary dictionary];
    for (NSString *name in rangeSels) {
        SEL s = NSSelectorFromString(name);
        if (![obj respondsToSelector:s]) continue;
        FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
        if (!FCB_structCall(obj, s, nil, &u)) continue;
        ranges[name] = @{@"start_s": @(FCB_seconds(u.start)),
                         @"duration_s": @(FCB_seconds(u.duration))};
    }
    for (NSString *name in timeSels) {
        SEL s = NSSelectorFromString(name);
        if (![obj respondsToSelector:s]) continue;
        FCB_CMTime t = {0,0,0,0};
        if (!FCB_structCall(obj, s, nil, &t) || t.timescale <= 0) continue;
        times[name] = @{@"s": @(FCB_seconds(t)),
                        @"v": @(t.value), @"ts": @(t.timescale)};
    }
    for (NSString *name in dblSels) {
        id v = FCB_probeDouble(obj, name);
        if (v) dbls[name] = v;
    }
    return @{@"label": label, @"class": NSStringFromClass([obj class]) ?: @"?",
             @"duration_s": @(FCB_timeOf(obj, @selector(duration))),
             @"ranges": ranges, @"times": times, @"doubles": dbls};
}

// Deep per-clip probe: the trim is not on FFAnchoredClip itself
// (unclippedRange/mediaRange report the FULL media; timelineRange reports
// the timeline position), so walk media -> clipRef -> first asset and probe
// each. Read-only, one retained handle, explicit selector lists.
static NSDictionary *FCB_debugRefs(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSMutableArray<NSValue *> *live = FCB_livePointers(sequence);
        if (![live containsObject:[NSValue valueWithPointer:(const void *)target]]) {
            result = @{@"error": [NSString stringWithFormat:@"stale id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSMutableDictionary *refs = [NSMutableDictionary dictionary];
        refs[@"clip"] = FCB_probeObject(target, @"clip");
        @try {
            id media = nil;
            SEL mediaSel = NSSelectorFromString(@"media");
            if ([target respondsToSelector:mediaSel])
                media = ((id (*)(id, SEL))objc_msgSend)(target, mediaSel);
            if (media) {
                refs[@"media"] = FCB_probeObject(media, @"media");
                id clipRef = nil;
                SEL crSel = NSSelectorFromString(@"clipRef");
                if ([target respondsToSelector:crSel])
                    clipRef = ((id (*)(id, SEL))objc_msgSend)(target, crSel);
                if (clipRef) {
                    refs[@"clipRef"] = FCB_probeObject(clipRef, @"clipRef");
                    id comp = nil;
                    SEL fvSel = NSSelectorFromString(@"firstVideoAnchoredComponent");
                    if (media && [media respondsToSelector:fvSel])
                        comp = ((id (*)(id, SEL))objc_msgSend)(media, fvSel);
                    if (!comp) {
                        SEL ctSel = NSSelectorFromString(@"componentForTrim");
                        if ([target respondsToSelector:ctSel])
                            comp = ((id (*)(id, SEL))objc_msgSend)(target, ctSel);
                    }
                    if (comp) refs[@"component"] = FCB_probeObject(comp, @"component");
                    // Sibling objects that may carry the source mapping:
                    // audio component (this timeline is audio), story items.
                    NSArray *getters = @[@"firstAudioAnchoredComponent",
                        @"storylineClip", @"anchoredToStoryItem", @"parentStoryItem",
                        @"storyline", @"primaryStoryItemComponent"];
                    for (NSString *gname in getters) {
                        SEL gs = NSSelectorFromString(gname);
                        id holder = ([target respondsToSelector:gs]) ? target
                            : ((media && [media respondsToSelector:gs]) ? media : nil);
                        if (!holder) continue;
                        id val = nil;
                        @try { val = ((id (*)(id, SEL))objc_msgSend)(holder, gs); }
                        @catch (NSException *e) { continue; }
                        if (!val) continue;
                        if ([val isKindOfClass:[NSArray class]] && [(NSArray *)val count] > 0)
                            val = [(NSArray *)val objectAtIndex:0];
                        if ([val isKindOfClass:[NSSet class]] && [(NSSet *)val count] > 0)
                            val = [[(NSSet *)val allObjects] objectAtIndex:0];
                        if (!val || [val isKindOfClass:[NSString class]] ||
                            [val isKindOfClass:[NSNumber class]]) continue;
                        @try { refs[gname] = FCB_probeObject(val, gname); }
                        @catch (NSException *e) {}
                    }
                    // The clip's own members (video/audio components) — the
                    // source mapping likely lives on FFAnchoredMediaComponent.
                    SEL ciSel = NSSelectorFromString(@"containedItems");
                    if ([target respondsToSelector:ciSel]) {
                        id inner = ((id (*)(id, SEL))objc_msgSend)(target, ciSel);
                        if ([inner isKindOfClass:[NSArray class]]) {
                            NSMutableArray *kids = [NSMutableArray array];
                            for (id sub in (NSArray *)inner)
                                [kids addObject:FCB_probeObject(sub, @"child")];
                            refs[@"children"] = kids;
                        }
                    }
                    id assets = nil;
                    SEL aSel = NSSelectorFromString(@"assets");
                    if ([clipRef respondsToSelector:aSel])
                        assets = ((id (*)(id, SEL))objc_msgSend)(clipRef, aSel);
                    NSArray *arr = nil;
                    if ([assets isKindOfClass:[NSSet class]]) arr = [(NSSet *)assets allObjects];
                    else if ([assets isKindOfClass:[NSArray class]]) arr = assets;
                    if ([arr count] > 0)
                        refs[@"asset0"] = FCB_probeObject([arr objectAtIndex:0], @"asset0");
                }
            }
        } @catch (NSException *e) {
            refs[@"walk_error"] = e.reason ?: @"exception";
        }
        result = @{@"id": cid, @"refs": refs};
    });
    return result;
}

static NSDictionary *FCB_debugRanges(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSMutableArray<NSValue *> *live = FCB_livePointers(sequence);
        if (![live containsObject:[NSValue valueWithPointer:(const void *)target]]) {
            result = @{@"error": [NSString stringWithFormat:@"stale id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSArray *rangeSels = @[@"unclippedRange", @"sourceRange", @"mediaRange",
            @"trimmedRange", @"sourceTimeRange", @"untrimmedRange", @"originalRange",
            @"availableRange", @"contentRange", @"clipRange", @"timelineRange",
            @"effectRange", @"audioRange", @"videoRange"];
        NSArray *timeSels = @[@"sourceStartTime", @"mediaStartTime", @"trimStartTime",
            @"startOffsetTime", @"sourceTime", @"mediaTime", @"unclippedStartTime",
            @"sourceAnchorTime", @"targetAnchorTime", @"localAnchorTime",
            @"timelineAnchorOffset", @"timelineParentAnchorOffset",
            @"localToParentOffset", @"parentToLocalOffset", @"timeOffset",
            @"offsetExportValue"];
        double dur = FCB_timeOf(target, @selector(duration));
        NSMutableDictionary *ranges = [NSMutableDictionary dictionary];
        NSMutableArray *rangeMiss = [NSMutableArray array];
        for (NSString *name in rangeSels) {
            SEL s = NSSelectorFromString(name);
            if (![target respondsToSelector:s]) { [rangeMiss addObject:name]; continue; }
            FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
            if (!FCB_structCall(target, s, nil, &u)) { [rangeMiss addObject:name]; continue; }
            ranges[name] = @{@"start_s": @(FCB_seconds(u.start)),
                             @"duration_s": @(FCB_seconds(u.duration))};
        }
        NSMutableDictionary *times = [NSMutableDictionary dictionary];
        NSMutableArray *timeMiss = [NSMutableArray array];
        for (NSString *name in timeSels) {
            SEL s = NSSelectorFromString(name);
            if (![target respondsToSelector:s]) { [timeMiss addObject:name]; continue; }
            FCB_CMTime t = {0,0,0,0};
            if (!FCB_structCall(target, s, nil, &t) || t.timescale <= 0) {
                [timeMiss addObject:name]; continue;
            }
            times[name] = @(FCB_seconds(t));
        }
        double start = -1;
        id primaryObj = nil;
        if ([sequence respondsToSelector:@selector(primaryObject)])
            primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
        if (primaryObj) {
            FCB_CMTimeRange r = {{0,0,0,0},{0,0,0,0}};
            if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), target, &r))
                start = FCB_seconds(r.start);
        }
        result = @{@"id": cid, @"class": NSStringFromClass([target class]) ?: @"?",
                   @"name": FCB_itemName(target),
                   @"timeline_start_s": @(start), @"duration_s": @(dur),
                   @"ranges": ranges, @"ranges_missing": rangeMiss,
                   @"times": times, @"times_missing": timeMiss};
    });
    return result;
}

// Mirror-image probe: the spine maps objects to TIMELINE ranges via
// effectiveRangeOfObject: — it may map to SOURCE ranges via a sibling
// selector. Try each on primaryObject and sequence with the clip as arg.
static NSDictionary *FCB_debugSpine(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        id primaryObj = nil;
        if ([sequence respondsToSelector:@selector(primaryObject)])
            primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
        NSArray *sels = @[@"effectiveRangeOfObject:", @"sourceRangeOfObject:",
            @"mediaRangeOfObject:", @"unclippedRangeOfObject:", @"storyRangeOfObject:",
            @"timelineRangeOfObject:", @"availableRangeOfObject:", @"contentRangeOfObject:",
            @"rangeOfObject:", @"timeRangeOfObject:", @"storyElementRangeOfObject:"];
        NSArray *hosts = primaryObj ? @[primaryObj, sequence] : @[sequence];
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        for (id host in hosts) {
            NSString *hkey = (host == (id)primaryObj) ? @"primaryObject" : @"sequence";
            NSMutableDictionary *hd = [NSMutableDictionary dictionary];
            hd[@"class"] = NSStringFromClass([host class]) ?: @"?";
            for (NSString *name in sels) {
                SEL s = NSSelectorFromString(name);
                if (![host respondsToSelector:s]) continue;
                FCB_CMTimeRange r = {{0,0,0,0},{0,0,0,0}};
                if (!FCB_structCall(host, s, target, &r)) continue;
                hd[name] = @{@"start_s": @(FCB_seconds(r.start)),
                             @"duration_s": @(FCB_seconds(r.duration))};
            }
            out[hkey] = hd;
        }
        result = @{@"id": cid, @"class": NSStringFromClass([target class]) ?: @"?",
                   @"hosts": out};
    });
    return result;
}

// Systematic probe: list method names on the object's class hierarchy
// matching a substring filter, with return-type encodings. Read-only,
// one retained handle. Use to discover the real trim/source API instead
// of guessing selector names.
static NSDictionary *FCB_debugMethods(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    NSString *filter = params[@"filter"];
    if (![filter isKindOfClass:[NSString class]]) filter = @"";
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        Class cls = [target class];
        int depth = 0;
        while (cls && depth < 8) {
            unsigned int n = 0;
            Method *ml = class_copyMethodList(cls, &n);
            NSMutableArray *names = [NSMutableArray array];
            for (unsigned int i = 0; i < n && [names count] < 300; i++) {
                NSString *name = NSStringFromSelector(method_getName(ml[i]));
                if ([filter length] > 0 &&
                    [name rangeOfString:filter options:NSCaseInsensitiveSearch].location == NSNotFound)
                    continue;
                char rt[8] = "?";
                @try {
                    NSMethodSignature *sig = [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(ml[i])];
                    if (sig) {
                        const char *t = [sig methodReturnType];
                        snprintf(rt, sizeof(rt), "%s", t ? t : "?");
                    }
                } @catch (NSException *e) {}
                [names addObject:[NSString stringWithFormat:@"%s %@", rt, name]];
            }
            if (ml) free(ml);
            out[NSStringFromClass(cls) ?: @"?"] = names;
            cls = class_getSuperclass(cls);
            depth++;
        }
        result = @{@"id": cid, @"filter": filter, @"hierarchy": out};
    });
    return result;
}

// Class-level probe: list method names (with return encodings) for a named
// class + superclasses. Needs no timeline object — pure runtime reflection.
static NSDictionary *FCB_debugClass(NSDictionary *params) {
    NSString *name = params[@"class"];
    if (![name isKindOfClass:[NSString class]] || [name length] == 0)
        return @{@"error": @"class parameter required (e.g. FFAnchoredMediaComponent)"};
    NSString *filter = params[@"filter"];
    if (![filter isKindOfClass:[NSString class]]) filter = @"";
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        Class cls = objc_getClass([name UTF8String]);
        if (!cls) { result = @{@"error": [NSString stringWithFormat:@"no class %@", name]}; return; }
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        int depth = 0;
        while (cls && depth < 10) {
            unsigned int n = 0;
            Method *ml = class_copyMethodList(cls, &n);
            NSMutableArray *names = [NSMutableArray array];
            for (unsigned int i = 0; i < n && [names count] < 500; i++) {
                NSString *mname = NSStringFromSelector(method_getName(ml[i]));
                if ([filter length] > 0 &&
                    [mname rangeOfString:filter options:NSCaseInsensitiveSearch].location == NSNotFound)
                    continue;
                char rt[16] = "?";
                @try {
                    NSMethodSignature *sig = [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(ml[i])];
                    if (sig) {
                        const char *t = [sig methodReturnType];
                        snprintf(rt, sizeof(rt), "%s", t ? t : "?");
                    }
                } @catch (NSException *e) {}
                [names addObject:[NSString stringWithFormat:@"%s %@", rt, mname]];
            }
            if (ml) free(ml);
            out[NSStringFromClass(cls) ?: @"?"] = names;
            cls = class_getSuperclass(cls);
            depth++;
        }
        result = @{@"class": name, @"filter": filter, @"hierarchy": out};
    });
    return result;
}

// Convert a clip's timeline edges through convertTime:toStoryline: /
// convertTime:fromStoryline:. If toStoryline(timeline_start) yields the
// source in-point, trim is directly computable with no more guessing.
static NSDictionary *FCB_debugConvert(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        double dur = FCB_timeOf(target, @selector(duration));
        double start = -1;
        id primaryObj = nil;
        if ([sequence respondsToSelector:@selector(primaryObject)])
            primaryObj = ((id (*)(id, SEL))objc_msgSend)(sequence, @selector(primaryObject));
        if (primaryObj) {
            FCB_CMTimeRange r = {{0,0,0,0},{0,0,0,0}};
            if (FCB_structCall(primaryObj, NSSelectorFromString(@"effectiveRangeOfObject:"), target, &r))
                start = FCB_seconds(r.start);
        }
        if (start < 0) { result = @{@"error": @"cannot resolve timeline start"}; return; }
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        for (NSString *name in @[@"convertTime:toStoryline:", @"convertTime:fromStoryline:"]) {
            SEL s = NSSelectorFromString(name);
            if (![target respondsToSelector:s]) { out[name] = @"missing"; continue; }
            NSMethodSignature *sig = [target methodSignatureForSelector:s];
            if (!sig || [sig numberOfArguments] != 3) { out[name] = @"bad-sig"; continue; }
            NSMutableDictionary *sd = [NSMutableDictionary dictionary];
            for (NSNumber *edge in @[@(start), @(start + dur)]) {
                double es = [edge doubleValue];
                FCB_CMTime inT;
                inT.value = (int64_t)(es * 600000.0);
                inT.timescale = 600000;
                inT.flags = 1;
                inT.epoch = 0;
                NSInvocation *inv = [NSInvocation invocationWithMethodSignature:sig];
                [inv setTarget:target];
                [inv setSelector:s];
                [inv setArgument:&inT atIndex:2];
                @try { [inv invoke]; }
                @catch (NSException *e) { sd[[edge stringValue]] = @"invoke-fail"; continue; }
                FCB_CMTime ret = {0,0,0,0};
                @try { [inv getReturnValue:&ret]; }
                @catch (NSException *e) { sd[[edge stringValue]] = @"read-fail"; continue; }
                sd[[edge stringValue]] = @{@"s": @(FCB_seconds(ret)),
                                           @"v": @(ret.value), @"ts": @(ret.timescale)};
            }
            out[name] = sd;
        }
        result = @{@"id": cid, @"timeline_start_s": @(start), @"duration_s": @(dur),
                   @"convert": out};
    });
    return result;
}

// Systematic getter sweep: call every no-arg object-returning method whose
// name smells like a sub-object, probe each result. Finds the per-clip
// component carrying the source mapping without guessing API names.
// Skips init/dealloc/new/copy families (never call those on live objects).
static NSDictionary *FCB_debugGetters(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSArray *smells = @[@"component", @"audio", @"video", @"element", @"child",
            @"member", @"part", @"source", @"media", @"clip", @"story", @"anchor",
            @"track", @"item", @"content", @"segment"];
        NSArray *banned = @[@"init", @"dealloc", @"new", @"copy", @"mutableCopy",
            @"retain", @"release", @"autorelease", @"set"];
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        NSMutableSet *seen = [NSMutableSet set];
        Class cls = [target class];
        int depth = 0;
        while (cls && depth < 10 && [out count] < 40) {
            unsigned int n = 0;
            Method *ml = class_copyMethodList(cls, &n);
            for (unsigned int i = 0; i < n && [out count] < 40; i++) {
                NSString *mname = NSStringFromSelector(method_getName(ml[i]));
                BOOL hit = NO;
                for (NSString *s in smells) {
                    if ([mname rangeOfString:s options:NSCaseInsensitiveSearch].location != NSNotFound) {
                        hit = YES; break;
                    }
                }
                if (!hit || [seen containsObject:mname]) continue;
                [seen addObject:mname];
                BOOL bad = NO;
                for (NSString *b in banned) {
                    if ([mname hasPrefix:b]) { bad = YES; break; }
                }
                if (bad) continue;
                NSMethodSignature *sig = nil;
                @try { sig = [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(ml[i])]; }
                @catch (NSException *e) { continue; }
                if (!sig || [sig numberOfArguments] != 2) continue;
                const char *rt = [sig methodReturnType];
                if (!rt || rt[0] != '@') continue;
                SEL s = NSSelectorFromString(mname);
                if (![target respondsToSelector:s]) continue;
                id val = nil;
                @try { val = ((id (*)(id, SEL))objc_msgSend)(target, s); }
                @catch (NSException *e) { continue; }
                if (!val) continue;
                if ([val isKindOfClass:[NSArray class]] && [(NSArray *)val count] > 0)
                    val = [(NSArray *)val objectAtIndex:0];
                else if ([val isKindOfClass:[NSSet class]] && [(NSSet *)val count] > 0)
                    val = [[(NSSet *)val allObjects] objectAtIndex:0];
                if (!val || [val isKindOfClass:[NSString class]] ||
                    [val isKindOfClass:[NSNumber class]] ||
                    [val isKindOfClass:[NSValue class]]) continue;
                @try { out[mname] = FCB_probeObject(val, mname); }
                @catch (NSException *e) {}
            }
            if (ml) free(ml);
            cls = class_getSuperclass(cls);
            depth++;
        }
        result = @{@"id": cid, @"getters": out};
    });
    return result;
}

// Dump EVERY member of anchoredTimelineItems plus alternate self-views,
// with the fields that matter. Decides the production trim strategy.
static NSDictionary *FCB_debugHops(NSDictionary *params) {
    NSString *cid = params[@"id"];
    if (![cid isKindOfClass:[NSString class]] || [cid length] == 0)
        return @{@"error": @"id parameter required (see timeline.clips)"};
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id timeline = FCB_activeTimeline();
        if (!timeline) { result = @{@"error": @"No active timeline."}; return; }
        id sequence = nil;
        if ([timeline respondsToSelector:@selector(sequence)])
            sequence = ((id (*)(id, SEL))objc_msgSend)(timeline, @selector(sequence));
        if (!sequence) { result = @{@"error": @"No sequence in timeline."}; return; }
        id target = sHandleObjs[cid];
        if (!target) {
            result = @{@"error": [NSString stringWithFormat:@"unknown id %@ — re-read timeline.clips", cid]};
            return;
        }
        NSMutableArray *members = [NSMutableArray array];
        @try {
            SEL aiSel = NSSelectorFromString(@"anchoredTimelineItems");
            if ([target respondsToSelector:aiSel]) {
                id arr = ((id (*)(id, SEL))objc_msgSend)(target, aiSel);
                NSArray *list = nil;
                if ([arr isKindOfClass:[NSArray class]]) list = arr;
                else if ([arr isKindOfClass:[NSSet class]]) list = [(NSSet *)arr allObjects];
                for (id hop in list ?: @[]) {
                    NSMutableDictionary *m = [NSMutableDictionary dictionary];
                    m[@"class"] = NSStringFromClass([hop class]) ?: @"?";
                    m[@"ptr"] = [NSString stringWithFormat:@"%p ==target:%@",
                        hop, (hop == target) ? @"YES" : @"NO"];
                    FCB_CMTimeRange tr = {{0,0,0,0},{0,0,0,0}};
                    if (FCB_structCall(hop, NSSelectorFromString(@"timelineRange"), nil, &tr))
                        m[@"timelineRange"] = @{@"start_s": @(FCB_seconds(tr.start)),
                                                @"duration_s": @(FCB_seconds(tr.duration))};
                    for (NSString *nm in @[@"sourceAnchorTime", @"localAnchorTime"]) {
                        SEL ts = NSSelectorFromString(nm);
                        if (![hop respondsToSelector:ts]) continue;
                        FCB_CMTime t = {0,0,0,0};
                        if (!FCB_structCall(hop, ts, nil, &t) || t.timescale <= 0) continue;
                        m[nm] = @(FCB_seconds(t));
                    }
                    [members addObject:m];
                }
            }
        } @catch (NSException *e) { members = [@[@{@"error": @"exception"}] mutableCopy]; }
        NSMutableDictionary *views = [NSMutableDictionary dictionary];
        for (NSString *gname in @[@"videoComponent", @"primaryItemComponent",
             @"primaryStoryItemComponent", @"storylineClip", @"inspectableAnchoredObject",
             @"asFFAnchoredObject", @"anchoredToStoryItem", @"storyline"]) {
            SEL gs = NSSelectorFromString(gname);
            if (![target respondsToSelector:gs]) continue;
            id val = nil;
            @try { val = ((id (*)(id, SEL))objc_msgSend)(target, gs); }
            @catch (NSException *e) { continue; }
            if (!val || [val isKindOfClass:[NSString class]] ||
                [val isKindOfClass:[NSNumber class]]) continue;
            NSMutableDictionary *vd = [NSMutableDictionary dictionary];
            vd[@"class"] = NSStringFromClass([val class]) ?: @"?";
            vd[@"isTarget"] = @((val == target) ? YES : NO);
            FCB_CMTime t = {0,0,0,0};
            if (FCB_structCall(val, NSSelectorFromString(@"sourceAnchorTime"), nil, &t)
                && t.timescale > 0)
                vd[@"sourceAnchorTime"] = @(FCB_seconds(t));
            views[gname] = vd;
        }
        result = @{@"id": cid, @"members": members, @"views": views};
    });
    return result;
}

static NSDictionary *FCB_timelineReleaseHandles(void) {    [sHandleObjs removeAllObjects];    [sHandlePtrs removeAllObjects];
    return @{@"released": @YES};
}

static NSDictionary *FCB_undoRedo(BOOL isUndo) {
    __block NSDictionary *result = nil;
    FCB_runOnMain(^{
        id app = ((id (*)(id, SEL))objc_msgSend)(
            (id)objc_getClass("NSApplication"), @selector(sharedApplication));
        id delegate = ((id (*)(id, SEL))objc_msgSend)(app, @selector(delegate));
        id library = nil;
        SEL libSel = NSSelectorFromString(@"_targetLibrary");
        if ([delegate respondsToSelector:libSel])
            library = ((id (*)(id, SEL))objc_msgSend)(delegate, libSel);
        if (!library) {
            id libs = ((id (*)(id, SEL))objc_msgSend)(
                (id)objc_getClass("FFLibraryDocument"), @selector(copyActiveLibraries));
            if ([libs respondsToSelector:@selector(firstObject)])
                library = ((id (*)(id, SEL))objc_msgSend)(libs, @selector(firstObject));
        }
        if (!library) { result = @{@"error": @"No library for undo"}; return; }
        id doc = ((id (*)(id, SEL))objc_msgSend)(library, @selector(libraryDocument));
        if (!doc) { result = @{@"error": @"No document for undo"}; return; }
        id um = ((id (*)(id, SEL))objc_msgSend)(doc, @selector(undoManager));
        if (!um) { result = @{@"error": @"No undo manager"}; return; }
        SEL canSel = isUndo ? @selector(canUndo) : @selector(canRedo);
        SEL goSel = isUndo ? @selector(undo) : @selector(redo);
        SEL nameSel = isUndo ? @selector(undoActionName) : @selector(redoActionName);
        BOOL can = ((BOOL (*)(id, SEL))objc_msgSend)(um, canSel);
        if (!can) { result = @{@"error": isUndo ? @"Nothing to undo" : @"Nothing to redo"}; return; }
        NSString *name = nil;
        @try { name = ((id (*)(id, SEL))objc_msgSend)(um, nameSel); } @catch (NSException *e) {}
        @try { ((void (*)(id, SEL))objc_msgSend)(um, goSel); }
        @catch (NSException *e) {
            result = @{@"error": [NSString stringWithFormat:@"Exception: %@", e.reason]};
            return;
        }
        result = @{@"action": isUndo ? @"undo" : @"redo", @"status": @"ok",
                   @"actionName": name ?: @""};
    });
    return result;
}

// ---------------------------------------------------------------- panel
// In-process Transcript panel: the clever shortcut around a Workflow
// Extension. Because this dylib already runs inside FCP (see patch_fcp.sh),
// a Window-menu item + WKWebView is all it takes — no Apple SDK download,
// no Xcode .appex target, no /Applications install + pluginkit registration,
// no sandbox network entitlement. FCP was re-signed with sandbox off, so
// plain localhost HTTP to the ui_server works from here.
//
// Shows MCP status until transcribed: the page itself renders /api/status +
// /api/editor edit_error. If make ui isn't running, a fallback page says so.

static int FCB_uiPort(void) {
    const char *e = getenv("TRANSCRIPT_UI_PORT");
    if (e && e[0]) {
        long p = strtol(e, NULL, 10);
        if (p > 0 && p < 65536) return (int)p;
    }
    return 8765;
}

@interface FCBTranscriptController : NSObject <WKNavigationDelegate>
@property (retain) NSWindow *window;
@property (retain) WKWebView *web;
+ (instancetype)shared;
- (void)openTranscript:(id)sender;
- (void)reloadTranscript:(id)sender;
@end

@implementation FCBTranscriptController

+ (instancetype)shared {
    static FCBTranscriptController *s = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ s = [[self alloc] init]; });
    return s;
}

- (NSURL *)serviceURL {
    return [NSURL URLWithString:
        [NSString stringWithFormat:@"http://127.0.0.1:%d/", FCB_uiPort()]];
}

- (void)openTranscript:(id)sender {
    @try {
        if (!self.window) {
            NSRect frame = NSMakeRect(0, 0, 400, 760);
            NSWindow *w = [[NSWindow alloc]
                initWithContentRect:frame
                styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                           NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
                backing:NSBackingStoreBuffered defer:NO];
            w.title = @"Transcript";
            w.minSize = NSMakeSize(300, 400);
            WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
            WKWebView *web = [[WKWebView alloc] initWithFrame:frame configuration:cfg];
            web.navigationDelegate = self;
            w.contentView = web;
            [w center];
            [w setFrameAutosaveName:@"FCPBridgeTranscript"];
            self.web = web;
            self.window = w;
        }
        [self.web loadRequest:[NSURLRequest requestWithURL:[self serviceURL]]];
        [self.window makeKeyAndOrderFront:nil];
    } @catch (NSException *e) {
        FCB_LOG(@"transcript panel failed: %@", e.reason);
    }
}

- (void)reloadTranscript:(id)sender {
    [self openTranscript:sender];
}

// Service down (make ui not running): replace the WebKit error page with
// actionable instructions + retry. Service up but no transcript is handled
// by the page itself (edit_error banner).
- (void)webView:(WKWebView *)webView
    didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    NSString *html = [NSString stringWithFormat:
        @"<body style='background:#0c0c0e;color:#c9c9cd;font:15px -apple-system,Helvetica,sans-serif;padding:40px 28px'>"
         "<h2 style='font-size:16px'>Transcript service not running</h2>"
         "<p>Start it, then retry:</p>"
         "<pre style='background:#131316;padding:10px 12px;border-radius:8px'>make ui  # http://127.0.0.1:%d</pre>"
         "<p style='color:#7a7a80'>Bridge %@ is up — this panel only needs the ui service. "
         "Once words are transcribed the cards appear here automatically.</p>"
         "<p><a href='http://127.0.0.1:%d/' style='color:#5ac8fa'>Retry</a></p></body>",
        FCB_uiPort(), FCB_VERSION, FCB_uiPort()];
    [webView loadHTMLString:html baseURL:nil];
}

- (void)webView:(WKWebView *)webView
    didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    [self webView:webView didFailProvisionalNavigation:navigation withError:error];
}

@end

static void FCB_installTranscriptMenu(void) {
    NSMenu *main = [NSApp mainMenu];
    if (!main) return;
    NSMenuItem *windowItem = nil;
    for (NSMenuItem *item in [main itemArray]) {
        if ([[item title] isEqualToString:@"Window"]) { windowItem = item; break; }
    }
    NSMenu *host = windowItem ? [windowItem submenu] : main;
    if (!host) return;
    for (NSMenuItem *item in [host itemArray]) {
        if ([[item title] isEqualToString:@"Transcript"]) return; // idempotent
    }
    if ([host numberOfItems] > 0) [host addItem:[NSMenuItem separatorItem]];
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:@"Transcript"
        action:@selector(openTranscript:) keyEquivalent:@"0"];
    item.target = [FCBTranscriptController shared];
    item.keyEquivalentModifierMask = NSEventModifierFlagCommand;
    item.toolTip = @"Open the transcript editor panel (needs `make ui` running)";
    [host addItem:item];
    FCB_LOG(@"Transcript panel installed (Window > Transcript)");
}

// ---------------------------------------------------------------- server

static NSDictionary *FCB_dispatch(NSString *method, NSDictionary *params) {
    if ([method isEqualToString:@"system.version"]) return FCB_systemVersion();
    if ([method isEqualToString:@"timeline.clips"]) return FCB_timelineClips();
    if ([method isEqualToString:@"timeline.select"]) return FCB_timelineSelect(params);
    if ([method isEqualToString:@"timeline.debug_ranges"]) return FCB_debugRanges(params);
    if ([method isEqualToString:@"timeline.debug_refs"]) return FCB_debugRefs(params);
    if ([method isEqualToString:@"timeline.debug_spine"]) return FCB_debugSpine(params);
    if ([method isEqualToString:@"timeline.debug_methods"]) return FCB_debugMethods(params);
    if ([method isEqualToString:@"timeline.debug_class"]) return FCB_debugClass(params);
    if ([method isEqualToString:@"timeline.debug_convert"]) return FCB_debugConvert(params);
    if ([method isEqualToString:@"timeline.debug_getters"]) return FCB_debugGetters(params);
    if ([method isEqualToString:@"timeline.debug_hops"]) return FCB_debugHops(params);
    if ([method isEqualToString:@"timeline.release_handles"]) return FCB_timelineReleaseHandles();
    if ([method isEqualToString:@"timeline.undo"]) return FCB_undoRedo(YES);
    if ([method isEqualToString:@"timeline.redo"]) return FCB_undoRedo(NO);
    if ([method isEqualToString:@"timeline.action"]) return FCB_timelineAction(params);
    if ([method isEqualToString:@"playback.position"]) return FCB_playbackPosition();
    if ([method isEqualToString:@"playback.seek"]) return FCB_playbackSeek(params);
    return @{@"error": [NSString stringWithFormat:@"unknown method: %@", method]};
}

static void FCB_handleClient(int fd) {
    FILE *stream = fdopen(fd, "r+");
    if (!stream) { close(fd); return; }
    char *line = NULL;
    size_t cap = 0;
    while (getline(&line, &cap, stream) > 0) {
        @autoreleasepool {
            NSData *data = [NSData dataWithBytes:line length:strlen(line)];
            NSDictionary *resp = nil;
            @try {
                NSDictionary *req = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
                NSString *method = req[@"method"];
                NSDictionary *params = req[@"params"];
                if (![params isKindOfClass:[NSDictionary class]]) params = @{};
                id reqId = req[@"id"] ?: [NSNull null];
                if (![method isKindOfClass:[NSString class]]) method = @"";
                NSDictionary *r = FCB_dispatch(method, params);
                if (r[@"error"])
                    resp = @{@"jsonrpc": @"2.0", @"id": reqId,
                             @"error": @{@"code": @(-32000), @"message": r[@"error"]}};
                else
                    resp = @{@"jsonrpc": @"2.0", @"id": reqId, @"result": r};
            } @catch (NSException *e) {
                resp = @{@"jsonrpc": @"2.0", @"id": [NSNull null],
                         @"error": @{@"code": @(-32700), @"message": @"bad request"}};
            }
            NSData *out = [NSJSONSerialization dataWithJSONObject:resp options:0 error:NULL];
            if (out) {
                fwrite([out bytes], 1, [out length], stream);
                fputc('\n', stream);
                fflush(stream);
            }
        }
    }
    free(line);
    fclose(stream); // closes fd
}

static dispatch_source_t sAcceptSource = nil;
static int sServerFd = -1;

static void FCB_startServer(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { FCB_LOG(@"socket failed: %s", strerror(errno)); return; }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(FCB_PORT);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        FCB_LOG(@"bind :%d failed: %s", FCB_PORT, strerror(errno));
        close(fd);
        return;
    }
    if (listen(fd, 5) < 0) { FCB_LOG(@"listen failed"); close(fd); return; }
    FCB_LOG(@"listening on 127.0.0.1:%d", FCB_PORT);
    // Retained in a file static: a dispatch source nobody owns is deallocated
    // at scope end, which cancels accepts while the socket stays bound —
    // connects then succeed and get silence. Don't "clean this up".
    sServerFd = fd;
    sAcceptSource = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, fd, 0,
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0));
    dispatch_source_t src = sAcceptSource;
    dispatch_source_set_event_handler(src, ^{
        int cfd = accept(fd, NULL, NULL);
        if (cfd < 0) return;
        int np = 1;
        setsockopt(cfd, SOL_SOCKET, SO_NOSIGPIPE, &np, sizeof(np));
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            FCB_handleClient(cfd);
        });
    });
    dispatch_source_set_cancel_handler(src, ^{ close(fd); });
    dispatch_resume(src);
}

__attribute__((constructor))
static void FCB_init(void) {
    // Constructor fires before FCP's frameworks exist; defer to launch.
    [[NSNotificationCenter defaultCenter]
        addObserverForName:NSApplicationDidFinishLaunchingNotification
        object:nil queue:nil usingBlock:^(NSNotification *note) {
            FCB_LOG(@"FCPBridge %@ loaded", FCB_VERSION);
            // Small delay so the app controller + libraries settle.
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC),
                dispatch_get_main_queue(), ^{
                    FCB_startServer();
                    @try { FCB_installTranscriptMenu(); }
                    @catch (NSException *e) {
                        FCB_LOG(@"transcript menu install failed: %@", e.reason);
                    }
                });
        }];
}
