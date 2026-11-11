#import "probe.h"
// Create a NEW empty library and open it as a normal FCP library (PEDocument, so the
// timeline can host its projects). Refuses if the path exists.
// args: {"path": "/abs/New Library.fcpbundle"}
NSDictionary *FCBProbe(NSDictionary *a){
  NSString *path=a[@"path"];
  if([[NSFileManager defaultManager] fileExistsAtPath:path]) return @{@"error":@"path exists; open it with open_library.m instead"};
  NSError *e=nil;
  id doc=((id(*)(id,SEL,id,BOOL,NSError**))objc_msgSend)([objc_getClass("PEDocument") alloc],NSSelectorFromString(@"initWithURL:createDefaultEvent:error:"),[NSURL fileURLWithPath:path],NO,&e);
  if(!doc) return @{@"error":D(e)};
  [[NSDocumentController sharedDocumentController] addDocument:doc];
  CALL0(doc,@"makeWindowControllers"); CALL0(doc,@"showWindows");
  NSMutableArray *open=[NSMutableArray new]; for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) [open addObject:D(CALL0(l,@"displayName"))];
  return @{@"status":@"created",@"docClass":NSStringFromClass([doc class]),@"open_libraries":open};
}
