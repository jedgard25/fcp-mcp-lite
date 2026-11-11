#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  id it=findItemNamed(CALL0(activeSequence(),@"primaryObject"),a[@"name"]);
  id pos=CALL0(xformOf(it),@"positionChannel3D"); id y=CALL0(pos,@"children")[[a[@"axis"] intValue]];
  NSArray *k=dumpKeys(y); NSMutableArray *s=[NSMutableArray new];
  double t0=[k[0][@"t"] doubleValue]; for(int f=0;f<8;f++) [s addObject:@(round(valAt(y,t0+f*1001.0/30000)*100)/100)];
  return @{@"keys":[k subarrayWithRange:NSMakeRange(0,MIN((NSUInteger)5,k.count))],@"perFrame":s,@"nkeys":@(k.count),@"curveInterp":@(((unsigned(*)(id,SEL))objc_msgSend)(y,NSSelectorFromString(@"curveInterpolation")))};
}
