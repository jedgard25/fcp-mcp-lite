#import "probe.h"
// Open an existing .fcpbundle through FCP's own document controller (PEDocument),
// the same path as File > Open Library. Never save/close libraries from here.
// args: {"path": "/abs/Lib.fcpbundle", "wait": 10}
NSDictionary *FCBProbe(NSDictionary *a){
  NSString *path=a[@"path"]; NSString *name=[[path lastPathComponent] stringByDeletingPathExtension];
  if(libNamed(name)) return @{@"status":@"already open",@"library":name};
  if(![[NSFileManager defaultManager] fileExistsAtPath:path]) return @{@"error":@"no such library"};
  [[NSDocumentController sharedDocumentController] openDocumentWithContentsOfURL:[NSURL fileURLWithPath:path] display:YES
    completionHandler:^(NSDocument *d, BOOL already, NSError *e){ if(e) NSLog(@"[probe] open_library error %@",e); }];
  NSDate *until=[NSDate dateWithTimeIntervalSinceNow:[a[@"wait"] doubleValue]?:10];
  while(!libNamed(name) && [until timeIntervalSinceNow]>0) [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.2]];
  NSMutableArray *open=[NSMutableArray new]; for(id l in CALL0(CLS("FFLibraryDocument"),@"copyActiveLibraries")) [open addObject:D(CALL0(l,@"displayName"))];
  return @{@"status":libNamed(name)?@"opened":@"not open yet",@"library":name,@"open_libraries":open};
}
