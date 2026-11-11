#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  id it=findItemNamed(CALL0(activeSequence(),@"primaryObject"),a[@"clip"]);
  id x=[CALL0(CALL0(xformOf(it),@"positionChannel3D"),@"children") firstObject];
  id pos=CALL0(xformOf(it),@"positionChannel3D");
  id ctrl=[[objc_getClass("FFChannelChangeController") alloc] init];
  ((void(*)(id,SEL,id,id,id))objc_msgSend)(ctrl,NSSelectorFromString(@"beginChannelChange:actionName:forItem:"),pos,@"WeftCut: easing (ctrl)",it);
  CALL1(ctrl,@"willSetChannel:",pos);
  void *k0=((void*(*)(id,SEL))objc_msgSend)(x,NSSelectorFromString(@"firstKeyframe"));
  void *k1=((void*(*)(id,SEL,void*))objc_msgSend)(x,NSSelectorFromString(@"nextKeyframe:"),k0);
  double dt=1.001, dv=50;
  ((void(*)(id,SEL,void*,double,double))objc_msgSend)(x,NSSelectorFromString(@"setKeyframeOutputTangents:outTanTime:outTanValue:"),k0,[a[@"ox"] doubleValue]*dt,[a[@"oy"] doubleValue]*dv);
  ((void(*)(id,SEL,void*,double,double))objc_msgSend)(x,NSSelectorFromString(@"setKeyframeInputTangents:inTanTime:inTanValue:"),k1,-(1-[a[@"ix"] doubleValue])*dt,-(1-[a[@"iy"] doubleValue])*dv);
  CALL1(ctrl,@"didSetChannel:",pos);
  NSError *e=nil; BOOL ok=((BOOL(*)(id,SEL,id,NSError**))objc_msgSend)(ctrl,NSSelectorFromString(@"endChannelChange:error:"),pos,&e);
  r[@"ok"]=@(ok); r[@"err"]=D(e); r[@"k0out"]=dumpKeys(x)[0][@"out"];
  r[@"undoName"]=D(CALL0(CALL0(CALL0(timelineModule(),@"document"),@"undoManager"),@"undoActionName"));
  return r;
}
