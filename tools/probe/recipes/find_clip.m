#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  id it=findItemNamed(CALL0(activeSequence(),@"primaryObject"),a[@"name"]);
  id um=CALL0(CALL0(timelineModule(),@"document"),@"undoManager");
  NSMutableArray *names=[NSMutableArray new]; for(id x in CALL0(CALL0(activeSequence(),@"primaryObject"),@"containedItems")) { [names addObject:D(CALL0(x,@"displayName"))]; for(id y in CALL0(x,@"anchoredItems")?:@[]) [names addObject:[@"  " stringByAppendingString:D(CALL0(y,@"displayName"))]]; }
  return @{@"found":D(it),@"undo":D(CALL0(um,@"undoActionName")),@"tree":names,@"seq":D(activeSequence())};
}
