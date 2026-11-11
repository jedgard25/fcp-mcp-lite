#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new]; id prim=CALL0(activeSequence(),@"primaryObject");
  for(NSDictionary *job in a[@"jobs"]){
    id it=findItemNamed(prim,job[@"name"]); if(!it){ r[job[@"name"]]=@"missing"; continue; }
    id pos=CALL0(xformOf(it),@"positionChannel3D"); NSArray *kids=CALL0(pos,@"children");
    NSMutableArray *v=[NSMutableArray new];
    for(NSNumber *t in job[@"t"]) [v addObject:@[@(valAt(kids[0],[t doubleValue])),@(valAt(kids[1],[t doubleValue]))]];
    r[job[@"name"]]=v; }
  return r;
}
