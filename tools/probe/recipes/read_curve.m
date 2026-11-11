#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  id it=findItemNamed(CALL0(activeSequence(),@"primaryObject"),a[@"clip"]);
  id x=[CALL0(CALL0(xformOf(it),@"positionChannel3D"),@"children") firstObject];
  r[@"curveInterp"]=@(((unsigned(*)(id,SEL))objc_msgSend)(x,NSSelectorFromString(@"curveInterpolation")));
  r[@"keys"]=dumpKeys(x);
  NSMutableArray *samp=[NSMutableArray new]; for(int i=0;i<=10;i++) [samp addObject:@(round(valAt(x,i*0.1001)*100)/100)]; r[@"samples_0_1s"]=samp;
  return r;
}
