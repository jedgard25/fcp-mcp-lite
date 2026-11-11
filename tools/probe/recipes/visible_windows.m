#import "probe.h"
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableArray *w=[NSMutableArray new];
  for(NSWindow *x in [NSApp windows]) if(x.isVisible) [w addObject:[NSString stringWithFormat:@"%@ '%@' sheet=%d panel=%d attached=%@",NSStringFromClass([x class]),x.title,x.isSheet,[x isKindOfClass:[NSPanel class]],D(x.attachedSheet)]];
  return @{@"modal":D([NSApp modalWindow]),@"windows":w};
}
