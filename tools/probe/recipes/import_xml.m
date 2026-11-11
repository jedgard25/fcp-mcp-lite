#import "probe.h"
static NSMutableArray *gModal;
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new]; gModal=[NSMutableArray new];
  // watchdog: abort any modal after N s, record its title
  NSTimer *wd=[NSTimer timerWithTimeInterval:[a[@"watchdog"] doubleValue]?:8 repeats:YES block:^(NSTimer *t){
    NSWindow *w=[NSApp modalWindow]; if(w){ [gModal addObject:[NSString stringWithFormat:@"%@ (%@)",w.title,NSStringFromClass([w class])]]; [NSApp abortModal]; }
    for(NSWindow *x in [NSApp windows]) if(x.isSheet&&x.isVisible) [gModal addObject:[@"sheet:" stringByAppendingString:x.title?:@""]];
  }];
  [[NSRunLoop mainRunLoop] addTimer:wd forMode:NSRunLoopCommonModes];
  [[NSRunLoop mainRunLoop] addTimer:wd forMode:NSModalPanelRunLoopMode];
  NSData *data=[NSData dataWithContentsOfFile:a[@"xml"]];
  id o=CALL0([objc_getClass("FFXMLImportOptions") alloc],@"initWithUserDefaults");
  if(a[@"libraryURL"]){ CALLV1(o,@"setLibraryURL:",[NSURL fileURLWithPath:a[@"libraryURL"]]);
    ((void(*)(id,SEL,BOOL))objc_msgSend)(o,NSSelectorFromString(@"setIsLibraryNew:"),[a[@"new"] boolValue]); }
  if(a[@"libraryName"]){ // existing open library by display name
    for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) if([D(CALL0(l,@"displayName")) isEqualToString:a[@"libraryName"]]) CALLV1(o,@"setLibrary:",l); }
  ((void(*)(id,SEL,BOOL))objc_msgSend)(o,NSSelectorFromString(@"setSuppressWarnings:"),YES);
  CALLV1(o,@"setDocumentURL:",[NSURL fileURLWithPath:a[@"xml"]]);
  CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent();
  NSArray *warn=nil; NSError *err=nil; id res=nil;
  @try{
    res=((id(*)(id,SEL,id,id,id,NSArray**,NSError**))objc_msgSend)(CLS("FFXML"),NSSelectorFromString(@"importFromXMLData:options:taskDelegate:warnings:error:"),data,o,nil,&warn,&err);
  }@catch(NSException *e){ r[@"exception"]=[NSString stringWithFormat:@"%@: %@",e.name,e.reason]; }
  [wd invalidate];
  r[@"ms"]=@((CFAbsoluteTimeGetCurrent()-t0)*1000);
  r[@"result"]=D(res); r[@"err"]=err?[NSString stringWithFormat:@"%@ %@",err.localizedDescription,err.userInfo]:@"nil";
  NSMutableArray *w=[NSMutableArray new]; for(id x in warn) [w addObject:D(x)]; r[@"warnings"]=w; r[@"modals"]=gModal;
  NSMutableArray *names=[NSMutableArray new]; for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) [names addObject:D(CALL0(l,@"displayName"))]; r[@"libs_after"]=names;
  return r;
}
