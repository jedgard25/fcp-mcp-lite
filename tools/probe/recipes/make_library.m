#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  NSURL *u=[NSURL fileURLWithPath:a[@"path"]];
  NSError *e=nil;
  id doc=((id(*)(id,SEL,id,BOOL,NSError**))objc_msgSend)([objc_getClass("FFLibraryDocument") alloc],NSSelectorFromString(@"initWithURL:createDefaultEvent:error:"),u,NO,&e);
  r[@"doc"]=D(doc); r[@"err"]=D(e);
  if(doc){ [[NSDocumentController sharedDocumentController] addDocument:doc];
    r[@"library"]=D(CALL0(doc,@"library"));
    r[@"exists"]=@([[NSFileManager defaultManager] fileExistsAtPath:a[@"path"]]); }
  NSMutableArray *names=[NSMutableArray new]; for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) [names addObject:D(CALL0(l,@"displayName"))]; r[@"libs_after"]=names;
  return r;
}
