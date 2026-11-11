// Group colors: color timeline clips (video + audio, compounds too) by a group tag in
// the clip note, independent of roles. Note token: fcpmcp:group=<Name>:<paletteIndex>
// e.g. "fcpmcp:group=Intro:9". Untagged clips keep FCP's normal role color.
//
// Mechanism: replaces +[FFColorForRole colorSchemeForRolesOfObject:] and
// +colorSchemeForRolesOfObject:usingVideoRolesWhenExpanded:, the class methods every
// timeline item asks for its color. Walks object -> -container (max 4 hops) for a
// tag via -[FFAnchoredObject metadataNotes], returns +colorSchemeForIndex:<idx> (FCP's
// own palette) or falls through to the original implementation.
// Install once per process (idempotent via env flag). Spike: loaded through dev.probe;
// production: compile into FCPBridge.dylib and install at launch.
#import "../../tools/probe/probe.h"

static IMP gOrigA, gOrigB, gOrigIdx;
static NSRegularExpression *gRe;

static id FCMGroupScheme(id obj) {
    if (getenv("FCPMCP_GROUPS_OFF")) return nil;
    id o = obj;
    for (int hop = 0; o && hop < 4; hop++) {
        if ([o respondsToSelector:NSSelectorFromString(@"metadataNotes")]) {
            NSString *n = ((id(*)(id,SEL))objc_msgSend)(o, NSSelectorFromString(@"metadataNotes"));
            if ([n isKindOfClass:[NSString class]] && [n rangeOfString:@"fcpmcp:group="].location != NSNotFound) {
                NSTextCheckingResult *m = [gRe firstMatchInString:n options:0 range:NSMakeRange(0, n.length)];
                if (m) {
                    long idx = [[n substringWithRange:[m rangeAtIndex:1]] integerValue];
                    return ((id(*)(id,SEL,long))gOrigIdx)((id)objc_getClass("FFColorForRole"),
                                                          NSSelectorFromString(@"colorSchemeForIndex:"), idx);
                }
            }
        }
        if (![o respondsToSelector:NSSelectorFromString(@"container")]) break;
        o = ((id(*)(id,SEL))objc_msgSend)(o, NSSelectorFromString(@"container"));
    }
    return nil;
}
static id hookA(id self, SEL _cmd, id obj) {
    id s = FCMGroupScheme(obj);
    return s ?: ((id(*)(id,SEL,id))gOrigA)(self, _cmd, obj);
}
static id hookB(id self, SEL _cmd, id obj, BOOL expanded) {
    id s = FCMGroupScheme(obj);
    return s ?: ((id(*)(id,SEL,id,BOOL))gOrigB)(self, _cmd, obj, expanded);
}

NSDictionary *FCBProbe(NSDictionary *a) {
    Class C = objc_getClass("FFColorForRole");
    if (!C) return @{@"error": @"FFColorForRole missing"};
    if (!getenv("FCPMCP_GROUP_HOOK")) {
        gRe = [NSRegularExpression regularExpressionWithPattern:@"fcpmcp:group=[^:\\s]+:(\\d+)" options:0 error:nil];
        gOrigIdx = method_getImplementation(class_getClassMethod(C, NSSelectorFromString(@"colorSchemeForIndex:")));
        gOrigA = method_setImplementation(class_getClassMethod(C, NSSelectorFromString(@"colorSchemeForRolesOfObject:")), (IMP)hookA);
        gOrigB = method_setImplementation(class_getClassMethod(C, NSSelectorFromString(@"colorSchemeForRolesOfObject:usingVideoRolesWhenExpanded:")), (IMP)hookB);
        setenv("FCPMCP_GROUP_HOOK", "1", 1);
    }
    if (a[@"off"]) { if ([a[@"off"] boolValue]) setenv("FCPMCP_GROUPS_OFF", "1", 1); else unsetenv("FCPMCP_GROUPS_OFF"); }
    id tm = timelineModule();
    if ([tm respondsToSelector:NSSelectorFromString(@"reloadTimelineView:")]) CALLV1(tm, @"reloadTimelineView:", nil);
    return @{@"installed": @YES, @"groupsOff": @(getenv("FCPMCP_GROUPS_OFF") != NULL)};
}
