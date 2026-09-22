// FCPBridge.m — minimal agent bridge for Final Cut Pro.
//
// One file, ~500 lines, 7 verbs. Patterns adapted from SpliceKit (MIT):
//   - NSApp.delegate -> activeEditorContainer -> timelineModule
//   - sequence -> primaryObject -> containedItems (spine walk)
//   - IBAction-style selectors on FFAnchoredTimelineModule with sender=nil
//   - NSInvocation for struct (CMTime) returns — ABI-safe on arm64 + x86_64
//   - TCP JSON-RPC 2.0 on 127.0.0.1:9876, newline-delimited
//
// Deliberately NOT included: runtime introspection, plugin loader, panels,
// transcription, captions, debug toolkit. Brains live in mcp/server.py;
// this file is dumb pipes with defensive respondsToSelector: checks.

#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <CoreMedia/CoreMedia.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <sys/socket.h>
#import <netinet/in.h>
#import <unistd.h>

#define FCB_PORT 9876
#define FCB_VERSION @"0.1.0"

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
    FCB_CMTimeRange u = {{0,0,0,0},{0,0,0,0}};
    if (FCB_structCall(item, NSSelectorFromString(@"unclippedRange"), nil, &u))
        trim = FCB_seconds(u.start);
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
    if (selected) d[@"selected"] = @([selected containsObject:item]);
    NSString *path = isMedia ? FCB_mediaPathForClip(item) : nil;
    if (path) d[@"media_path"] = path;
    [out addObject:d];
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

static NSDictionary *FCB_timelineReleaseHandles(void) {
    [sHandleObjs removeAllObjects];
    [sHandlePtrs removeAllObjects];
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

// ---------------------------------------------------------------- server

static NSDictionary *FCB_dispatch(NSString *method, NSDictionary *params) {
    if ([method isEqualToString:@"system.version"]) return FCB_systemVersion();
    if ([method isEqualToString:@"timeline.clips"]) return FCB_timelineClips();
    if ([method isEqualToString:@"timeline.select"]) return FCB_timelineSelect(params);
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
                dispatch_get_main_queue(), ^{ FCB_startServer(); });
        }];
}
