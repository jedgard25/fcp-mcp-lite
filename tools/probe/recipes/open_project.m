#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  id seq=seqNamed(a[@"lib"],a[@"event"],a[@"name"]); if(!seq) return @{@"error":@"no seq"};
  id cont=CALL0(appDelegate(),@"activeEditorContainer"); CALL1(cont,@"loadEditorForSequence:",seq);
  return @{@"active":D(activeSequence())};
}
