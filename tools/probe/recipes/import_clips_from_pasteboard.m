#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  NSString *xml=[NSString stringWithContentsOfFile:a[@"xml"] encoding:NSUTF8StringEncoding error:nil];
  NSPasteboard *pb=[NSPasteboard pasteboardWithUniqueName]; [pb clearContents]; [pb setString:xml forType:@"com.apple.finalcutpro.xml.v1-13"];
  id task=CALL1(CLS("FFXMLTranslationTask"),@"translationTaskForPasteboard:",pb);
  id o=CALL0([objc_getClass("FFXMLImportOptions") alloc],@"initWithUserDefaults");
  CALLV1(o,@"setLibrary:",libNamed(a[@"lib"])); CALLV1(o,@"setEvent:",eventNamed(a[@"lib"],a[@"event"]));
  ((void(*)(id,SEL,BOOL))objc_msgSend)(o,NSSelectorFromString(@"setSuppressWarnings:"),YES);
  CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent();
  BOOL ok=((BOOL(*)(id,SEL,id))objc_msgSend)(task,NSSelectorFromString(@"importClipsWithOptions:"),o);
  r[@"ms"]=@((CFAbsoluteTimeGetCurrent()-t0)*1000); r[@"ok"]=@(ok); r[@"err"]=D(CALL0(task,@"error")); r[@"warn"]=D(CALL0(task,@"warnings"));
  id cr=CALL0(task,@"allImportedClipsAndRanges"); r[@"clipsAndRanges"]=[NSString stringWithFormat:@"%@ %@",NSStringFromClass([cr class]),D(cr)];
  r[@"FFPasteboard+"]=findSels(objc_getClass("FFPasteboard"),@"",YES);
  NSMutableArray *ps=[NSMutableArray new]; for(NSString *s in findSels(objc_getClass("FFPasteboard"),@"",NO)) if([s rangeOfString:@"write" options:1].location!=NSNotFound||[s rangeOfString:@"init" options:1].location!=NSNotFound) [ps addObject:s]; r[@"FFPasteboard-"]=ps;
  return r;
}
