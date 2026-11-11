#import "probe.h"
// Close an open library the way File > Close Library does (FFWorkspace), letting FCP
// save it itself. Never call saveDocument:/close on library documents directly.
// args: {"name": "LibraryDisplayName", "wait": 10}
NSDictionary *FCBProbe(NSDictionary *a){
  id lib=libNamed(a[@"name"]); if(!lib) return @{@"status":@"not open"};
  Class W=objc_getClass("FFWorkspace");
  id ws=CALL0((id)W,@"sharedWorkspace")?:CALL0((id)W,@"defaultWorkspace")?:CALL0((id)W,@"workspace");
  if(!ws) return @{@"error":@"no workspace",@"class+":findSels(W,@"",YES)};
  __block BOOL done=NO;
  ((void(*)(id,SEL,id,id))objc_msgSend)(ws,NSSelectorFromString(@"closeLibrary:completionBlock:"),lib,^(){ done=YES; });
  NSDate *until=[NSDate dateWithTimeIntervalSinceNow:[a[@"wait"] doubleValue]?:10];
  while(libNamed(a[@"name"]) && [until timeIntervalSinceNow]>0) [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.2]];
  NSMutableArray *open=[NSMutableArray new]; for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) [open addObject:D(CALL0(l,@"displayName"))];
  return @{@"status":libNamed(a[@"name"])?@"still open":@"closed",@"completion":@(done),@"open_libraries":open};
}
