#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  id ev = a[@"lib"] ? eventNamed(a[@"lib"],a[@"event"]) : nil;
  if(!ev){ id proj=CALL0(activeSequence(),@"project"); id evs=CALL1(CLS("FFXMLTranslation"),@"libraryEventsForProjects:",@[proj]); ev=[([evs isKindOfClass:[NSSet class]]?[evs allObjects]:evs) firstObject]; }
  CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent(); NSError *e=nil;
  NSData *d=exportEvents(@[ev],&e);
  [d writeToFile:a[@"out"] atomically:YES];
  return @{@"ms":@((CFAbsoluteTimeGetCurrent()-t0)*1000),@"bytes":@(d.length),@"err":D(e)};
}
