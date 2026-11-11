#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  id tm=timelineModule(); id cont=CALL0(appDelegate(),@"activeEditorContainer");
  id um=CALL0(CALL0(tm,@"document"),@"undoManager"); r[@"umClass"]=NSStringFromClass([um class]);
  NSString *xml=[NSString stringWithContentsOfFile:a[@"xml"] encoding:NSUTF8StringEncoding error:nil];
  NSPasteboard *pb=[NSPasteboard generalPasteboard];
  [um beginUndoGrouping]; [um setActionName:@"WeftCut: B-roll batch"];
  NSArray *times=a[@"times"]; CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent();
  for(NSNumber *t in times){ [pb clearContents]; [pb setString:[xml stringByReplacingOccurrencesOfString:@"PASTED_B" withString:[NSString stringWithFormat:@"PASTED_B_%@",t]] forType:@"com.apple.finalcutpro.xml.v1-13"];
    ((void(*)(id,SEL,CMTime))objc_msgSend)(cont,NSSelectorFromString(@"setPlayheadSequenceTime:"),CMT([t doubleValue])); CALL1(tm,@"pasteAnchored:",nil); }
  [um setActionName:@"WeftCut: B-roll batch"]; [um endUndoGrouping];
  r[@"ms"]=@((CFAbsoluteTimeGetCurrent()-t0)*1000); r[@"undoName"]=D(CALL0(um,@"undoActionName")); r[@"groupLevel"]=@([um groupingLevel]);
  // inspector display string for E_posEase x at key0: check unit formatting
  id it=findItemNamed(CALL0(activeSequence(),@"primaryObject"),@"PASTED_IMG_1255");
  id x=[CALL0(CALL0(xformOf(it),@"positionChannel3D"),@"children") firstObject];
  void *k0=((void*(*)(id,SEL))objc_msgSend)(x,NSSelectorFromString(@"firstKeyframe")); BOOL isDef=NO;
  id pair=((id(*)(id,SEL,id,void*,BOOL,BOOL*))objc_msgSend)(x,NSSelectorFromString(@"newValueStringPairFromChannel:withKeyframe:isRadians:isDefault:"),x,k0,NO,&isDef);
  r[@"displayPair_x_key0"]=D(pair);
  return r;
}
