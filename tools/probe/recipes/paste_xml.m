#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  NSString *xml=[NSString stringWithContentsOfFile:a[@"xml"] encoding:NSUTF8StringEncoding error:nil];
  NSPasteboard *pb=[NSPasteboard generalPasteboard]; [pb clearContents];
  [pb setString:xml forType:a[@"type"]?:@"com.apple.finalcutpro.xml.v1-13"];
  id tm=timelineModule(); id cont=CALL0(appDelegate(),@"activeEditorContainer");
  if(a[@"at"]) ((void(*)(id,SEL,CMTime))objc_msgSend)(cont,NSSelectorFromString(@"setPlayheadSequenceTime:"),CMT([a[@"at"] doubleValue]));
  id um=CALL0(CALL0(tm,@"document"),@"undoManager"); NSString *before=D(CALL0(um,@"undoActionName"));
  NSString *sel=a[@"sel"]?:@"pasteAnchored:";
  BOOL valid=YES; if([tm respondsToSelector:@selector(validateUserInterfaceItem:)]){ NSMenuItem *mi=[[NSMenuItem alloc] initWithTitle:@"x" action:NSSelectorFromString(sel) keyEquivalent:@""]; valid=((BOOL(*)(id,SEL,id))objc_msgSend)(tm,@selector(validateUserInterfaceItem:),mi); }
  r[@"validated"]=@(valid);
  CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent();
  CALLV1(tm,sel,nil);
  r[@"ms"]=@((CFAbsoluteTimeGetCurrent()-t0)*1000);
  r[@"undoBefore"]=before; r[@"undoAfter"]=D(CALL0(um,@"undoActionName"));
  r[@"found"]=D(findItemNamed(CALL0(activeSequence(),@"primaryObject"),@"PASTED_IMG_1255"));
  return r;
}
