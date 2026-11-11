#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <CoreMedia/CoreMedia.h>
#import <objc/runtime.h>
#import <objc/message.h>
#define MSG0(o,s) ((id(*)(id,SEL))objc_msgSend)((id)(o),NSSelectorFromString(@s))
#define MSG1(o,s,a) ((id(*)(id,SEL,id))objc_msgSend)((id)(o),NSSelectorFromString(@s),(id)(a))
#define CLS(n) ((id)objc_getClass(n))
static inline NSString *D(id o){ return o? [[o description] substringToIndex:MIN((NSUInteger)300,[[o description] length])] : @"nil"; }
static inline id appDelegate(void){ return [NSApp delegate]; }
static inline id timelineModule(void){ id c=MSG0(appDelegate(),"activeEditorContainer"); return c? MSG0(c,"timelineModule"):nil; }
static inline id activeSequence(void){ id tm=timelineModule(); return tm? MSG0(tm,"sequence"):nil; }
static NSString *sigOf(Class c, NSString *sel, BOOL meta){
  Method m = meta? class_getClassMethod(c, NSSelectorFromString(sel)) : class_getInstanceMethod(c, NSSelectorFromString(sel));
  return m? @(method_getTypeEncoding(m)) : @"(none)";
}
static NSArray *findSels(Class c, NSString *needle, BOOL meta){
  NSMutableArray *a=[NSMutableArray new]; unsigned n=0;
  Method *ms=class_copyMethodList(meta? object_getClass(c):c,&n);
  for(unsigned i=0;i<n;i++){ NSString *s=NSStringFromSelector(method_getName(ms[i]));
    if(!needle.length || [s rangeOfString:needle options:NSCaseInsensitiveSearch].location!=NSNotFound)
      [a addObject:[NSString stringWithFormat:@"%@%@ %s",meta?@"+":@"-",s,method_getTypeEncoding(ms[i])]]; }
  free(ms); return [a sortedArrayUsingSelector:@selector(compare:)];
}
// which class (walking up) implements selector
static NSString *implementer(Class c, NSString *sel){
  SEL s=NSSelectorFromString(sel);
  for(Class k=c;k;k=class_getSuperclass(k)){ unsigned n=0; Method *ms=class_copyMethodList(k,&n); BOOL f=NO;
    for(unsigned i=0;i<n;i++) if(method_getName(ms[i])==s){f=YES;break;} free(ms); if(f) return NSStringFromClass(k);} return nil;
}
static inline id CALL0(id o, NSString *s){ SEL x=NSSelectorFromString(s); if(!o||![o respondsToSelector:x]) return nil; return ((id(*)(id,SEL))objc_msgSend)(o,x); }
static inline id CALL1(id o, NSString *s, id a){ SEL x=NSSelectorFromString(s); if(!o||![o respondsToSelector:x]) return nil; return ((id(*)(id,SEL,id))objc_msgSend)(o,x,a); }
static inline id libNamed(NSString *n){ for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) if([D(CALL0(l,@"displayName")) isEqualToString:n]) return l; return nil; }
static inline id eventNamed(NSString *lib, NSString *ev){ return CALL1(libNamed(lib),@"eventForName:",ev); }
static inline id seqNamed(NSString *lib, NSString *ev, NSString *name){ for(id s in CALL0(eventNamed(lib,ev),@"items")) if([D(CALL0(s,@"displayName")) isEqualToString:name]) return s; return nil; }
static NSData *exportEvents(NSArray *events, NSError **err){
  id opts=CALL0([objc_getClass("FFXMLExportOptions") alloc],@"initWithUserDefaults");
  return ((id(*)(id,SEL,id,id,id,NSError**))objc_msgSend)(CLS("FFXML"),NSSelectorFromString(@"newXMLDataForEvents:withOptions:taskDelegate:error:"),events,opts,nil,err);
}
static id findItemNamed(id container, NSString *name){
  for(NSString *g in @[@"containedItems",@"anchoredItems"]){
    for(id it in (CALL0(container,g)?:@[])){ if([D(CALL0(it,@"displayName")) isEqualToString:name]) return it; id f=findItemNamed(it,name); if(f) return f; } }
  return nil;
}
static inline CMTime CMT(double s){ return CMTimeMakeWithSeconds(s, 30000); }
static NSArray *dumpKeys(id ch){
  NSMutableArray *out=[NSMutableArray new];
  void *k=((void*(*)(id,SEL))objc_msgSend)(ch,NSSelectorFromString(@"firstKeyframe"));
  while(k){ CMTime t; double v=0, it=0,iv=0,ot=0,ov=0;
    ((BOOL(*)(id,SEL,void*,CMTime*,double*))objc_msgSend)(ch,NSSelectorFromString(@"getKeyframe:time:value:"),k,&t,&v);
    unsigned interp=((unsigned(*)(id,SEL,void*))objc_msgSend)(ch,NSSelectorFromString(@"segmentInterpolation:"),k);
    ((void(*)(id,SEL,void*,double*,double*))objc_msgSend)(ch,NSSelectorFromString(@"getKeyframeInputTangents:inTanTime:inTanValue:"),k,&it,&iv);
    ((void(*)(id,SEL,void*,double*,double*))objc_msgSend)(ch,NSSelectorFromString(@"getKeyframeOutputTangents:outTanTime:outTanValue:"),k,&ot,&ov);
    [out addObject:@{@"t":@(CMTimeGetSeconds(t)),@"v":@(v),@"interp":@(interp),@"in":@[@(it),@(iv)],@"out":@[@(ot),@(ov)]}];
    k=((void*(*)(id,SEL,void*))objc_msgSend)(ch,NSSelectorFromString(@"nextKeyframe:"),k); }
  return out;
}
static double valAt(id ch,double s){ return ((double(*)(id,SEL,CMTime))objc_msgSend)(ch,NSSelectorFromString(@"curveDoubleValueAtTime:"),CMT(s)); }
static id xformOf(id it){ for(id e in CALL0(CALL0(it,@"videoEffects"),@"intrinsicEffects")) if([D(CALL0(e,@"effectID")) isEqualToString:@"HEXForm3D"]) return e; return nil; }
