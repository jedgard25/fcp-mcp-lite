#import "probe.h"
static NSString *savePNG(NSView *v, NSString *path){
  NSRect b=v.bounds; if(b.size.width<2||b.size.height<2) return @"empty";
  NSBitmapImageRep *rep=[v bitmapImageRepForCachingDisplayInRect:b]; [v cacheDisplayInRect:b toBitmapImageRep:rep];
  NSData *d=[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}]; [d writeToFile:path atomically:YES];
  return [NSString stringWithFormat:@"%.0fx%.0f %lu bytes",b.size.width,b.size.height,(unsigned long)d.length];
}
static void collect(NSView *v, NSMutableArray *out, int depth){
  NSString *c=NSStringFromClass([v class]);
  if(depth<12 && ([c rangeOfString:@"Timeline"].location!=NSNotFound||[c rangeOfString:@"Viewer"].location!=NSNotFound||[c rangeOfString:@"Player"].location!=NSNotFound||[c rangeOfString:@"Metal"].location!=NSNotFound))
    [out addObject:@{@"cls":c,@"w":@(v.bounds.size.width),@"h":@(v.bounds.size.height),@"view":v}];
  for(NSView *s in v.subviews) collect(s,out,depth+1);
}
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  NSWindow *w=nil; for(NSWindow *x in [NSApp windows]) if([x.title isEqualToString:@"Final Cut Pro"]) w=x;
  r[@"window"]=savePNG(w.contentView,[a[@"dir"] stringByAppendingPathComponent:@"fcp_window.png"]);
  NSMutableArray *vs=[NSMutableArray new]; collect(w.contentView,vs,0);
  NSMutableArray *list=[NSMutableArray new]; int i=0;
  for(NSDictionary *d in vs){ if([d[@"w"] doubleValue]>300 && [d[@"h"] doubleValue]>100){ NSString *p=[a[@"dir"] stringByAppendingPathComponent:[NSString stringWithFormat:@"v%02d_%@.png",i++,d[@"cls"]]]; [list addObject:[NSString stringWithFormat:@"%@ %@",d[@"cls"],savePNG(d[@"view"],p)]]; } else [list addObject:[NSString stringWithFormat:@"%@ %@x%@ (skipped)",d[@"cls"],d[@"w"],d[@"h"]]]; }
  r[@"views"]=list; return r;
}
